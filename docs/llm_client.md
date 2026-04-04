# Algorithm Summary: `llm_client.py`

## 1. Role in the Pipeline

This script is a crucial library module that acts as an abstraction layer for interacting with various Large Language Model (LLM) and embedding model APIs. It provides a consistent, unified interface that the [SummaryEngine](../summary_engine/README.md) can use without needing to know the specific details of the underlying API being called.

It enables the project to seamlessly switch between different model providers (like OpenAI, DeepSeek, or a local Ollama instance) while providing a high-performance, persistent caching layer. It also provides a fake client for testing.

## 2. Design and Architecture

The script uses a combination of **Factory and Strategy** patterns, enriched with a **Centralized Async Worker** to handle high-concurrency I/O efficiently.

### Strategy Pattern

*   **`LlmClient` / `EmbeddingClient`**: Abstract base classes that define common interfaces (`generate_summary`, `generate_embeddings`).
*   **`LiteLlmClient(LlmClient)`**: A unified implementation powered by the **LiteLLM** library. This single class replaces provider-specific implementations, handling OpenAI, DeepSeek, and Ollama through a standardized backend.
*   **`FakeLlmClient(LlmClient)`**: A polymorphic mock client used for testing. It returns hardcoded text bypassing the L2 caching system.
*   **`SentenceTransformerClient(EmbeddingClient)`**: A local implementation for generating vector embeddings using the `sentence-transformers` library.

### Centralized Async Worker (The "Sidecar" Loop)

To support massive concurrency (e.g., 100+ remote workers) without resource exhaustion, the module implements a producer-consumer bridge:
*   **Thread-to-Async Bridge**: While the main pipeline uses many threads, all LLM API calls and disk cache operations are offloaded to a **single background event loop** running in a dedicated thread.
*   **Concurrency Management**: Uses an `asyncio.Semaphore` (dynamically matched to the number of worker threads) to manage active requests, preventing OS-level resource spikes and honoring provider rate limits.
*   **Resource Efficiency**: This design specifically prevents "File Descriptor Explosion." By confining SQLite (via `diskcache`) to a single thread, the number of open files is drastically reduced compared to a traditional multi-threaded approach.

### Factory Pattern

*   **`setup_llm_client()`**: Decouples the application logic from client creation and cache initialization. It initializes the background worker and sets the internal semaphore based on the requested concurrency.

## 3. Caching Strategy (L2 Cache)

The project implements a robust, two-tier caching system to minimize costs and latency:
*   **L1 Cache (Node Level)** (see [Summary Engine](../summary_engine/README.md)): Managed by `SummaryCacheManager`, storing high-level summaries for graph nodes.
*   **L2 Cache (LLM Level)**: Managed by `LlmCacheManager` using `diskcache.FanoutCache`.
    *   **Prompt-Based Identity**: Keys are SHA-256 hashes of the full prompt, making the cache model-agnostic and resilient across different runs.
    *   **Non-Blocking I/O**: Cache lookups and writes are offloaded to a thread pool executor (`loop.run_in_executor`), ensuring the event loop never stalls during disk operations.
    *   **Persistence**: Uses a "Promotion-on-Success" strategy and `atexit` registration to ensure SQLite integrity and clean resource release.

## 4. Supported Models

### LLM Providers (via LiteLLM)

*   **OpenAI**: Configurable via `OPENAI_MODEL` and `OPENAI_API_KEY`.
*   **DeepSeek**: Configurable via `DEEPSEEK_MODEL` and `DEEPSEEK_API_KEY`.
*   **Ollama**: Configurable via `OLLAMA_MODEL` and `OLLAMA_BASE_URL`.
*   **Fake**: No need to configure. It is the default provider if no other provider is specified.

### Embedding Models

*   **SentenceTransformer**: Runs locally on CPU/GPU. It automatically downloads and caches models (e.g., `all-MiniLM-L6-v2`) on first use.
