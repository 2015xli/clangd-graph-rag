import re
import tiktoken
from typing import List, Tuple

# ---------------------------------------------------------------
# 1️⃣  Sanitize / unsanitize special tokens
# ---------------------------------------------------------------

_SPECIAL_PATTERN = re.compile(r"<\|[^|]+?\|>")

def sanitize_special_tokens(text: str) -> str:
    """Break up special tokens so the model won't treat them as control tokens."""
    return _SPECIAL_PATTERN.sub(lambda m: f"< |{m.group(0)[2:-2]}| >", text)

def unsanitize_special_tokens(text: str) -> str:
    """Restore text that was sanitized earlier."""
    return re.sub(r"< \|([^|]+?)\| >", r"<|\1|>", text)

# ---------------------------------------------------------------
# 2️⃣  Chunker using tiktoken
# ---------------------------------------------------------------

class SafeCodeChunker:
    def __init__(self, model: str = "gpt-4-turbo", chunk_size: int = 8000):
        # Create tokenizer (uses the same tokenizer as the given model)
        self.enc = tiktoken.encoding_for_model(model)
        self.chunk_size = chunk_size

    def chunk_text(self, source_code: str) -> List[Tuple[int, int, str]]:
        """
        Encode, chunk, decode.
        Returns list of (start_token, end_token, chunk_text).
        """
        safe_text = sanitize_special_tokens(source_code)
        tokens = self.enc.encode(safe_text)  # We don't want to use disallowed_special=(), because it may hide the issue that bites us

        chunks = []
        for i in range(0, len(tokens), self.chunk_size):
            sub_tokens = tokens[i : i + self.chunk_size]
            decoded = self.enc.decode(sub_tokens)
            chunks.append((i, i + len(sub_tokens), decoded))

        return chunks

# ---------------------------------------------------------------
# 3️⃣  Example usage
# ---------------------------------------------------------------

if __name__ == "__main__":
    code = r'''
    // <|endoftext|> example
    std::string s = "<|fim_prefix|>"; 
    int main() { return 0; }
    '''

    chunker = SafeCodeChunker(model="gpt-4-turbo", chunk_size=20)
    chunks = chunker.chunk_text(code)

    for start, end, text in chunks:
        print(f"\n--- Chunk {start}:{end} ---")
        print(text)

    # If you later want the exact original:
    restored = unsanitize_special_tokens(chunks[0][2])
    print("\nRestored snippet:\n", restored)

