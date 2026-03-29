#!/usr/bin/env python3
"""
This module provides a client for interacting with various LLM APIs using LiteLLM,
with support for an L2 disk cache to persist query/response pairs.
It uses a centralized background event loop to manage concurrency and prevent
file descriptor explosion when using FanoutCache.
"""

import os
import logging
import litellm
import hashlib
import shutil
import json
import asyncio
import threading
import atexit
import math
from typing import Optional, Any
import resource

logger = logging.getLogger(__name__)
litellm.set_verbose = False
logger.setLevel(logging.WARNING)

# --- Cache Management ---

class LlmCacheManager:
    """
    Manages a persistent disk cache for LLM responses using FanoutCache.
    Acts as an L2 cache layer.
    """
    def __init__(self, folder: str, shards: int = 8, size_limit: str = "2GB", reset: bool = False):
        self.folder = folder
        self.shards = shards
        self.size_limit_bytes = self._parse_size_to_bytes(size_limit)
        
        if reset:
            self.clear_cache()

        self.check_file_count()
        self.check_cache_settings()

        try:
            from diskcache import FanoutCache
            self.cache = FanoutCache(
                directory=self.folder, 
                shards=self.shards, 
                size_limit=self.size_limit_bytes
            )
            logger.info(f"LlmCacheManager initialized at {self.folder} (Size limit: {size_limit}, Shards: {shards})")
        except ImportError:
            logger.error("The 'diskcache' package is required for LLM caching. Please run 'pip install diskcache'.")
            self.cache = None

    def check_file_count(self):
        """
        In the new async architecture, only the background event loop thread
        opens connections to the cache. The FD requirement is now much lower.
        Needed FDs = (1 thread * shards * ~3 FDs) + overhead.
        """
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        
        # Base requirement: shards * 3 (for SQLite) + room for network sockets and files
        file_count_needed = (self.shards * 3) + 150
        
        if hard < file_count_needed:
            logger.error(f"File count hard limit {hard} is too low. Needed: {file_count_needed}.")
            exit(1)

        if soft < file_count_needed:
            logger.warning(f"File count soft limit {soft} is low. Increasing to {file_count_needed}.")
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (file_count_needed, hard))
            except Exception as e:
                logger.error(f"Failed to increase soft limit: {e}")

    def check_cache_settings(self):
        meta_path = os.path.join(self.folder, "cache_meta_data.json")
        
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r') as f:
                    meta = json.load(f)
                    ori_shards = meta.get("shards")
                    if ori_shards != self.shards:
                        logger.error(f"Cache shard count mismatch. New: {self.shards}, Old: {ori_shards}.")
                        exit(1)
            except Exception as e:
                logger.warning(f"Could not read cache meta data: {e}. Proceeding.")
        else:
            if os.path.exists(self.folder):
                self.clear_cache()

            os.makedirs(self.folder, exist_ok=True)
            with open(meta_path, 'w') as f:
                json.dump({"shards": self.shards, "size_limit": self.size_limit_bytes}, f)
                
        return True
        
    def _parse_size_to_bytes(self, size_str: str) -> int:
        units = {"KB": 1024, "MB": 1024**2, "GB": 1024**3}
        size_str = size_str.upper().strip()
        
        for unit, multiplier in units.items():
            if size_str.endswith(unit):
                try:
                    number = float(size_str[:-len(unit)])
                    return int(number * multiplier)
                except ValueError:
                    break
        return 2 * 1024**3

    def clear_cache(self):
        logger.info(f"Clearing LLM cache at {self.folder}...")
        if os.path.exists(self.folder):
            try:
                shutil.rmtree(self.folder)
            except Exception as e:
                logger.error(f"Failed to remove cache directory: {e}")
                exit(1)

    def get_instance(self):
        return self.cache

# --- LLM Clients ---

class LlmClient:
    """
    Base class for LLM clients. Manages a singleton background event loop 
    to handle all async operations (API calls and DiskCache) centrally.
    """
    is_local: bool = False
    _worker_loop: Optional[asyncio.AbstractEventLoop] = None
    _worker_thread: Optional[threading.Thread] = None
    _lock = threading.Lock()
    _semaphore: Optional[asyncio.Semaphore] = None

    def __init__(self):
        self.cache = None

    @classmethod
    def launch_worker_thread(cls, concurrency_limit: int):
        """Initializes the background worker thread and event loop exactly once."""
        with cls._lock:
            if cls._worker_thread is None:
                cls._worker_loop = asyncio.new_event_loop()
                cls._semaphore = asyncio.Semaphore(concurrency_limit)
                cls._worker_thread = threading.Thread(
                    target=cls._run_event_loop, 
                    args=(cls._worker_loop,), 
                    daemon=True,
                    name="LlmClientWorker"
                )
                cls._worker_thread.start()
                atexit.register(cls.terminate)
                logger.debug(f"LlmClient background worker thread started with concurrency limit {concurrency_limit}.")

    @staticmethod
    def _run_event_loop(loop):
        asyncio.set_event_loop(loop)
        loop.run_forever()

    @classmethod
    def terminate(cls):
        """Gracefully shuts down the background loop and thread."""
        with cls._lock:
            if cls._worker_loop is not None and cls._worker_loop.is_running():
                cls._worker_loop.call_soon_threadsafe(cls._worker_loop.stop)
                if cls._worker_thread:
                    cls._worker_thread.join(timeout=2)
                cls._worker_loop = None
                cls._worker_thread = None
                logger.info("LlmClient background worker terminated.")

    def enable_system_cache(self, cache_manager: LlmCacheManager):
        self.cache = cache_manager.get_instance()

    def generate_summary(self, prompt: str) -> str:
        """Synchronous entry point that bridges to the async background worker."""
        if self._worker_loop is None or not self._worker_loop.is_running():
            logger.error("LlmClient worker loop is not running.")
            return ""

        future = asyncio.run_coroutine_threadsafe(
            self._async_generate_wrapper(prompt), 
            self._worker_loop
        )
        try:
            # We use a long timeout here as the internal acompletion has its own timeout
            return future.result(timeout=310)
        except Exception as e:
            logger.error(f"LlmClient request failed: {e}")
            return ""

    async def _async_generate_wrapper(self, prompt: str) -> str:
        """Internal wrapper to handle caching logic before/after the model call."""
        cache_key = None
        if self.cache is not None:
            prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            cache_key = f"Prompt_hash:{prompt_hash}"
            
            # Non-blocking cache check
            cached_val = await self._worker_loop.run_in_executor(
                None, lambda: self.cache.get(cache_key)
            )
            if cached_val:
                return cached_val

        # Cache miss: Call the implementation-specific generator
        async with self._semaphore:
            content = await self._async_generate(prompt)

        # Save to DiskCache
        if content and self.cache is not None:
            await self._worker_loop.run_in_executor(
                None, lambda: self.cache.set(cache_key, content)
            )
        
        return content

    async def _async_generate(self, prompt: str) -> str:
        """To be implemented by subclasses."""
        raise NotImplementedError

class LiteLlmClient(LlmClient):
    """A unified client for various LLM APIs using LiteLLM."""
    def __init__(self, api_name: str):
        self.api_name = api_name.lower()
        self.is_local = self.api_name == 'ollama'
        super().__init__()
        
        if self.api_name == 'openai':
            self.model_name = os.environ.get("OPENAI_MODEL", "gpt-3.5-turbo")
        elif self.api_name == 'deepseek':
            self.model_name = os.environ.get("DEEPSEEK_MODEL", "deepseek-coder")
        elif self.api_name == 'ollama':
            self.model_name = f"ollama/{os.environ.get('OLLAMA_MODEL', 'deepseek-llm:7b')}"
        else:
            raise ValueError(f"Unsupported API '{self.api_name}' for LiteLlmClient.")

    async def _async_generate(self, prompt: str) -> str:
        try:
            response = await litellm.acompletion(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                timeout=300
            )
            return response.choices[0].message.content
        except Exception as e:
            logger.error(f"LiteLLM acompletion failed for model '{self.model_name}': {e}")
            return ""

class FakeLlmClient(LlmClient):
    """A fake client for debugging that returns a static summary."""
    def __init__(self):
        self.api_name = "fake"
        self.is_local = True 
        super().__init__()

    async def _async_generate(self, prompt: str) -> str:
        """Simulate an async delay and return static text."""
        # await asyncio.sleep(0.01) 
        return "This part implements important functionalities."


def get_llm_client(api_name: str) -> LlmClient:
    """Factory function to get an LLM client."""
    api_name = api_name.lower()
    if api_name == 'fake':
        return FakeLlmClient()
    
    try:
        return LiteLlmClient(api_name)
    except ValueError:
         raise ValueError(f"Unknown API: {api_name}. Supported APIs are: openai, deepseek, ollama, fake.")


# --- Embedding Clients ---
# Stays synchronous as it's typically CPU/GPU bound locally.

class EmbeddingClient:
    """Base class for embedding clients."""
    is_local: bool = False

    def generate_embeddings(self, texts: list[str], show_progress_bar: bool = True) -> list[list[float]]:
        raise NotImplementedError

class SentenceTransformerClient(EmbeddingClient):
    """Client that uses a local SentenceTransformer model."""
    is_local: bool = True

    def __init__(self):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError("The 'sentence-transformers' package is required. Run 'pip install sentence-transformers'.")
        
        model_name = os.environ.get("SENTENCE_TRANSFORMER_MODEL", "all-MiniLM-L6-v2")
        logger.info(f"Loading local SentenceTransformer model: {model_name}")
        self.model = SentenceTransformer(model_name)

    def generate_embeddings(self, texts: list[str], show_progress_bar: bool = True) -> list[list[float]]:
        embeddings = self.model.encode(texts, show_progress_bar=show_progress_bar)
        return [emb.tolist() for emb in embeddings]


def get_embedding_client(api_name: str) -> EmbeddingClient:
    logger.info("Initializing local SentenceTransformer client for embeddings.")
    return SentenceTransformerClient()
