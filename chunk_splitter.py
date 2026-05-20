"""
Chunk splitter: split text into chunks of max 1000 characters.
Target ~800 chars, splits at sentence boundaries (. ! ? ;) when possible.
"""

import re
import sys
from pathlib import Path


def split_chunks(text: str, max_len: int = 1000, target_len: int = 800) -> list[str]:
    """Split text into chunks ≤ max_len chars, targeting target_len (~800).
    Splits at sentence boundaries (. ! ? ;) when possible."""
    # Normalize whitespace
    text = re.sub(r'\r\n', '\n', text)
    text = re.sub(r'\n{2,}', '\n\n', text)
    
    # Split into sentences first
    # Vietnamese sentence endings: . ! ? and sometimes ;
    sentences = re.split(r'(?<=[.!?;])\s+', text)
    
    chunks = []
    current = ""
    
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        
        # If single sentence > max_len, split by comma or force-split
        if len(sentence) > max_len:
            # Try splitting by comma
            parts = re.split(r'(?<=,)\s+', sentence)
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                if len(part) > max_len:
                    # Force split at max_len
                    while len(part) > max_len:
                        # Find last space before max_len
                        split_pos = part[:max_len].rfind(' ')
                        if split_pos < 100:
                            split_pos = max_len
                        if current:
                            chunks.append(current.strip())
                            current = ""
                        chunks.append(part[:split_pos].strip())
                        part = part[split_pos:].strip()
                    if part:
                        if current and len(current) + 1 + len(part) <= target_len:
                            current += " " + part
                        else:
                            if current:
                                chunks.append(current.strip())
                            current = part
                elif current and len(current) + 1 + len(part) <= target_len:
                    current += " " + part
                else:
                    if current:
                        chunks.append(current.strip())
                    current = part
        elif current and len(current) + 1 + len(sentence) <= target_len:
            current += " " + sentence
        else:
            if current:
                chunks.append(current.strip())
            current = sentence
    
    if current.strip():
        chunks.append(current.strip())
    
    return chunks


if __name__ == "__main__":
    input_file = sys.argv[1] if len(sys.argv) > 1 else "/Users/phamvanhuy/elevenlabs-re/1 Tin trong ngày.txt"
    max_len = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
    target_len = int(sys.argv[3]) if len(sys.argv) > 3 else 800
    
    text = Path(input_file).read_text(encoding='utf-8')
    chunks = split_chunks(text, max_len, target_len)
    
    # Validate
    over = [i for i, c in enumerate(chunks) if len(c) > max_len]
    
    print(f"Total chunks: {len(chunks)}")
    print(f"Max chunk len: {max(len(c) for c in chunks)}")
    print(f"Min chunk len: {min(len(c) for c in chunks)}")
    print(f"Avg chunk len: {sum(len(c) for c in chunks) / len(chunks):.0f}")
    if over:
        print(f"WARNING: {len(over)} chunks exceed {max_len} chars!")
    else:
        print(f"All chunks ≤ {max_len} chars ✓")
    
    # Save chunks
    out_file = Path(input_file).stem + "_chunks.txt"
    out_path = Path(input_file).parent / out_file
    with open(out_path, 'w', encoding='utf-8') as f:
        for i, chunk in enumerate(chunks):
            f.write(f"---CHUNK {i+1}---\n{chunk}\n")
    
    print(f"\nSaved to: {out_path}")
    print(f"\nFirst 3 chunks:")
    for i, chunk in enumerate(chunks[:3]):
        print(f"  [{i+1}] ({len(chunk)} chars): {chunk[:80]}...")
