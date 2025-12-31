#!/usr/bin/env python3
"""
This module provides a client for interacting with various LLM APIs.
"""

import os
import logging
import requests # NOTE: This script requires the 'requests' library to be installed.

logger = logging.getLogger(__name__)

# --- Summarization Clients ---

class LlmClient:
    """Base class for LLM clients."""
    is_local: bool = False

    def generate_summary(self, prompt: str) -> str:
        """Generates a summary for a given prompt."""
        raise NotImplementedError

class OpenAiClient(LlmClient):
    """Client for OpenAI's API."""
    def __init__(self):
        self.api_key = os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY environment variable not set.")
        self.api_url = "https://api.openai.com/v1/chat/completions"
        self.model = os.environ.get("OPENAI_MODEL", "gpt-3.5-turbo")

    def generate_summary(self, prompt: str) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}]
        }
        try:
            response = requests.post(self.api_url, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
            return response.json()['choices'][0]['message']['content']
        except requests.RequestException as e:
            logger.error(f"OpenAI API request failed: {e}")
            return ""

class DeepSeekClient(LlmClient):
    """Client for DeepSeek's API."""
    def __init__(self):
        self.api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise ValueError("DEEPSEEK_API_KEY environment variable not set.")
        self.api_url = "https://api.deepseek.com/chat/completions"
        self.model = os.environ.get("DEEPSEEK_MODEL", "deepseek-coder")

    def generate_summary(self, prompt: str) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}]
        }
        try:
            response = requests.post(self.api_url, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
            return response.json()['choices'][0]['message']['content']
        except requests.RequestException as e:
            logger.error(f"DeepSeek API request failed: {e}")
            return ""

class OllamaClient(LlmClient):
    """Client for a local Ollama instance."""
    is_local: bool = True

    def __init__(self):
        self.base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        if not self.base_url:
            raise ValueError("OLLAMA_BASE_URL environment variable not set.")
        self.api_url = f"{self.base_url.rstrip('/')}/api/generate"
        self.model = os.environ.get("OLLAMA_MODEL", "codellama")

    def generate_summary(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False
        }
        try:
            response = requests.post(self.api_url, json=payload, timeout=300)
            response.raise_for_status()
            return response.json()['response']
        except requests.RequestException as e:
            logger.error(f"Ollama API request failed: {e}")
            return ""

class FakeLlmClient(LlmClient):
    """A fake client for debugging that returns a static summary. Acts as remote API service"""
    #is_local: bool = True

    def generate_summary(self, prompt: str) -> str:
        """Returns a hardcoded summary for any prompt."""
        return "This part implements important functionalities."


def get_llm_client(api_name: str) -> LlmClient:
    """Factory function to get an LLM client."""
    api_name = api_name.lower()
    if api_name == 'openai':
        return OpenAiClient()
    elif api_name == 'deepseek':
        return DeepSeekClient()
    elif api_name == 'ollama':
        return OllamaClient()
    elif api_name == 'fake':
        return FakeLlmClient()
    else:
        raise ValueError(f"Unknown API: {api_name}. Supported APIs are: openai, deepseek, ollama, fake.")

# --- Embedding Clients ---
# NOTE: The SentenceTransformerClient requires 'sentence-transformers' and 'torch'
# to be installed. Please run: pip install sentence-transformers

class EmbeddingClient:
    """Base class for embedding clients."""
    is_local: bool = False

    def generate_embeddings(self, texts: list[str], show_progress_bar: bool = True) -> list[list[float]]:
        """Generates embedding vectors for a given list of texts."""
        raise NotImplementedError

class SentenceTransformerClient(EmbeddingClient):
    """Client that uses a local SentenceTransformer model."""
    is_local: bool = True

    def __init__(self):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError("The 'sentence-transformers' package is required for local embeddings. Please run 'pip install sentence-transformers' to install it.")
        
        model_name = os.environ.get("SENTENCE_TRANSFORMER_MODEL", "all-MiniLM-L6-v2")
        logger.info(f"Loading local SentenceTransformer model: {model_name}")
        # The model will be downloaded on first use and cached by the library.
        self.model = SentenceTransformer(model_name)
        logger.info("SentenceTransformer model loaded successfully.")

    def generate_embeddings(self, texts: list[str], show_progress_bar: bool = True) -> list[list[float]]:
        """
        Generates embedding vectors for a given list of texts.
        
        Args:
            texts: List of text strings to embed
            show_progress_bar: Whether to show a progress bar during encoding
            
        Returns:
            List of embedding vectors as lists of floats
        """
        # The encode method can show its own progress bar, which is useful for large batches.
        embeddings = self.model.encode(texts, show_progress_bar=show_progress_bar)
        # Convert numpy arrays to standard lists for JSON/Neo4j compatibility
        return [emb.tolist() for emb in embeddings]


def get_embedding_client(api_name: str) -> EmbeddingClient:
    """Factory function to get an embedding client."""
    # The api_name can be used in the future to select different embedding models/APIs
    # For now, we default to the local sentence-transformer for all cases.
    logger.info("Initializing local SentenceTransformer client for embeddings.")
    return SentenceTransformerClient()
