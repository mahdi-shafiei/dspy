import functools
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

import ujson
from litellm import Router
from litellm.router import RetryPolicy

from dspy.clients.finetune import FinetuneJob, TrainingMethod
from dspy.clients.lm_finetune_utils import execute_finetune_job, get_provider_finetune_job_class
from dspy.utils.callback import BaseCallback, with_callbacks

from .base_lm import BaseLM

logger = logging.getLogger(__name__)


class LM(BaseLM):
    """
    A language model supporting chat or text completion requests for use with DSPy modules.
    """

    def __init__(
        self,
        model: str,
        model_type: Literal["chat", "text"] = "chat",
        temperature: float = 0.0,
        max_tokens: int = 1000,
        cache: bool = True,
        launch_kwargs: Optional[Dict[str, Any]] = None,
        callbacks: Optional[List[BaseCallback]] = None,
        num_retries: int = 8,
        **kwargs,
    ):
        """
        Create a new language model instance for use with DSPy modules and programs.

        Args:
            model: The model to use. This should be a string of the form ``"llm_provider/llm_name"``
                   supported by LiteLLM. For example, ``"openai/gpt-4o"``.
            model_type: The type of the model, either ``"chat"`` or ``"text"``.
            temperature: The sampling temperature to use when generating responses.
            max_tokens: The maximum number of tokens to generate per response.
            cache: Whether to cache the model responses for reuse to improve performance
                   and reduce costs.
            callbacks: A list of callback functions to run before and after each request.
            num_retries: The number of times to retry a request if it fails transiently due to
                         network error, rate limiting, etc. Requests are retried with exponential
                         backoff.
        """
        # Remember to update LM.copy() if you modify the constructor!
        self.model = model
        self.model_type = model_type
        self.cache = cache
        self.launch_kwargs = launch_kwargs or {}
        self.kwargs = dict(temperature=temperature, max_tokens=max_tokens, **kwargs)
        self.history = []
        self.callbacks = callbacks or []
        self.num_retries = num_retries

        # TODO: Arbitrary model strings could include the substring "o1-". We
        # should find a more robust way to check for the "o1-" family models.
        if "o1-" in model:
            assert (
                max_tokens >= 5000 and temperature == 1.0
            ), "OpenAI's o1-* models require passing temperature=1.0 and max_tokens >= 5000 to `dspy.LM(...)`"

    @with_callbacks
    def __call__(self, prompt=None, messages=None, **kwargs):
        # Build the request.
        cache = kwargs.pop("cache", self.cache)
        messages = messages or [{"role": "user", "content": prompt}]
        kwargs = {**self.kwargs, **kwargs}

        # Make the request and handle LRU & disk caching.
        if self.model_type == "chat":
            completion = cached_litellm_completion if cache else litellm_completion
        else:
            completion = cached_litellm_text_completion if cache else litellm_text_completion

        response = completion(
            request=ujson.dumps(dict(model=self.model, messages=messages, **kwargs)),
            num_retries=self.num_retries,
        )
        outputs = [c.message.content if hasattr(c, "message") else c["text"] for c in response["choices"]]

        # Logging, with removed api key & where `cost` is None on cache hit.
        kwargs = {k: v for k, v in kwargs.items() if not k.startswith("api_")}
        entry = dict(prompt=prompt, messages=messages, kwargs=kwargs, response=response)
        entry = dict(**entry, outputs=outputs, usage=dict(response["usage"]))
        entry = dict(**entry, cost=response.get("_hidden_params", {}).get("response_cost"))
        entry = dict(
            **entry,
            timestamp=datetime.now().isoformat(),
            uuid=str(uuid.uuid4()),
            model=self.model,
            model_type=self.model_type,
        )
        self.history.append(entry)
        self.update_global_history(entry)

        return outputs

    def launch(self):
        """Send a request to the provider to launch the model, if needed."""
        msg = f"`launch()` is called for the auto-launched model {self.model}"
        msg += " -- no action is taken!"
        logger.info(msg)

    def kill(self):
        """Send a request to the provider to kill the model, if needed."""
        msg = f"`kill()` is called for the auto-launched model {self.model}"
        msg += " -- no action is taken!"
        logger.info(msg)

    def finetune(
        self,
        train_data: List[Dict[str, Any]],
        train_kwargs: Optional[Dict[str, Any]] = None,
        train_method: TrainingMethod = TrainingMethod.SFT,
        provider: str = "openai",
        cache_finetune: bool = True,
    ) -> FinetuneJob:
        """Start model fine-tuning, if supported."""
        from dspy import settings as settings

        err = "Fine-tuning is an experimental feature."
        err += " Set `dspy.settings.experimental` to `True` to use it."
        assert settings.experimental, err

        FinetuneJobClass = get_provider_finetune_job_class(provider=provider)
        finetune_job = FinetuneJobClass(
            model=self.model,
            train_data=train_data,
            train_kwargs=train_kwargs,
            train_method=train_method,
            provider=provider,
        )

        executor = ThreadPoolExecutor(max_workers=1)
        executor.submit(execute_finetune_job, finetune_job, lm=self, cache_finetune=cache_finetune)
        executor.shutdown(wait=False)

        return finetune_job

    def copy(self, **kwargs):
        """Returns a copy of the language model with possibly updated parameters."""

        import copy

        new_instance = copy.deepcopy(self)
        new_instance.history = []

        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(new_instance, key, value)
            if (key in self.kwargs) or (not hasattr(self, key)):
                new_instance.kwargs[key] = value

        return new_instance


@functools.lru_cache(maxsize=None)
def cached_litellm_completion(request, num_retries: int):
    return litellm_completion(
        request,
        cache={"no-cache": False, "no-store": False},
        num_retries=num_retries,
    )


def litellm_completion(request, num_retries: int, cache={"no-cache": True, "no-store": True}):
    kwargs = ujson.loads(request)
    router = _get_litellm_router(model=kwargs["model"], num_retries=num_retries)
    return router.completion(
        cache=cache,
        **kwargs,
    )


@functools.lru_cache(maxsize=None)
def _get_litellm_router(model: str, num_retries: int) -> Router:
    """
    Get a LiteLLM router for the given model with the specified number of retries
    for transient errors.

    Args:
        model: The name of the LiteLLM model to query (e.g. 'openai/gpt-4').
        num_retries: The number of times to retry a request if it fails transiently due to
                     network error, rate limiting, etc. Requests are retried with exponential
                     backoff.
    Returns:
        A LiteLLM router instance that can be used to query the given model.
    """
    retry_policy = RetryPolicy(
        TimeoutErrorRetries=num_retries,
        RateLimitErrorRetries=num_retries,
        InternalServerErrorRetries=num_retries,
        # We don't retry on errors that are unlikely to be transient
        # (e.g. bad request, invalid auth credentials)
        BadRequestErrorRetries=0,
        AuthenticationErrorRetries=0,
        ContentPolicyViolationErrorRetries=0,
    )

    return Router(
        # LiteLLM routers must specify a `model_list`, which maps model names passed
        # to `completions()` into actual LiteLLM model names. For our purposes, the
        # model name is the same as the LiteLLM model name, so we add a single
        # entry to the `model_list` that maps the model name to itself
        model_list=[
            {
                "model_name": model,
                "litellm_params": {
                    "model": model,
                },
            }
        ],
        retry_policy=retry_policy,
    )


@functools.lru_cache(maxsize=None)
def cached_litellm_text_completion(request, num_retries: int):
    return litellm_text_completion(
        request,
        num_retries=num_retries,
        cache={"no-cache": False, "no-store": False},
    )


def litellm_text_completion(request, num_retries: int, cache={"no-cache": True, "no-store": True}):
    kwargs = ujson.loads(request)

    # Extract the provider and model from the model string.
    # TODO: Not all the models are in the format of "provider/model"
    model = kwargs.pop("model").split("/", 1)
    provider, model = model[0] if len(model) > 1 else "openai", model[-1]
    text_completion_model_name = f"text-completion-openai/{model}"

    # Use the API key and base from the kwargs, or from the environment.
    api_key = kwargs.pop("api_key", None) or os.getenv(f"{provider}_API_KEY")
    api_base = kwargs.pop("api_base", None) or os.getenv(f"{provider}_API_BASE")

    # Build the prompt from the messages.
    prompt = "\n\n".join([x["content"] for x in kwargs.pop("messages")] + ["BEGIN RESPONSE:"])

    router = _get_litellm_router(model=text_completion_model_name, num_retries=num_retries)
    return router.text_completion(
        cache=cache,
        model=text_completion_model_name,
        api_key=api_key,
        api_base=api_base,
        prompt=prompt,
        **kwargs,
    )
