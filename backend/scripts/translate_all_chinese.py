#!/usr/bin/env python3
"""
Translate all Chinese comments, docstrings, and non-LLM prompt strings
in backend Python files into English.

Strategy:
  - Parse each file with tokenize to find STRING and COMMENT tokens.
  - For tokens that contain Chinese characters, translate them.
  - Replace in-place.

Dependencies: pip install deep-translator
"""

import os
import re
import sys
import tokenize
import io

try:
    from deep_translator import GoogleTranslator
except ImportError:
    print("Installing deep-translator...")
    os.system(f"{sys.executable} -m pip install deep-translator -q")
    from deep_translator import GoogleTranslator

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHINESE_RE = re.compile(r'[\u4e00-\u9fa5]')

# Files to skip entirely (already done or should not be touched)
SKIP_FILES = {
    'translate_all_chinese.py',
    'translate_backend.py',
    'scan_chinese.py',
}

# LLM Prompt variable names — these contain Chinese ON PURPOSE as user-facing
# Chinese content that shouldn't be translated (they are bilingual prompts).
# We translate everything EXCEPT these variables.
SKIP_VARS = set()  # We will translate everything including prompts

translator = GoogleTranslator(source='zh-CN', target='en')

def translate(text: str) -> str:
    """Translate Chinese text to English, preserving non-Chinese parts."""
    if not CHINESE_RE.search(text):
        return text
    try:
        # Split by newlines to handle multiline strings better
        lines = text.split('\n')
        translated_lines = []
        for line in lines:
            if CHINESE_RE.search(line):
                try:
                    t = translator.translate(line.strip())
                    if t:
                        # Preserve leading whitespace
                        leading = len(line) - len(line.lstrip())
                        translated_lines.append(line[:leading] + t)
                    else:
                        translated_lines.append(line)
                except Exception:
                    translated_lines.append(line)
            else:
                translated_lines.append(line)
        return '\n'.join(translated_lines)
    except Exception as e:
        print(f"  Translation error: {e}")
        return text


def translate_file(fpath: str) -> int:
    """Translate Chinese in a Python file. Returns number of replacements."""
    with open(fpath, encoding='utf-8') as f:
        source = f.read()

    if not CHINESE_RE.search(source):
        return 0

    # We'll do line-by-line replacement for comments,
    # and token-based replacement for strings.
    lines = source.split('\n')
    new_lines = lines[:]
    replacements = 0

    # ── Pass 1: translate inline comments (# ...) ──────────────────────────
    comment_re = re.compile(r'(#\s*)(.*)')
    for i, line in enumerate(lines):
        # Find comment portion (not inside a string)
        # Simple heuristic: find the last # that's a real comment
        stripped = line
        # Check if line has chinese in a comment
        if '#' in line and CHINESE_RE.search(line):
            # Find comment start (skip # inside strings is hard; use simple approach)
            idx = line.find('#')
            before = line[:idx]
            comment_part = line[idx:]
            if CHINESE_RE.search(comment_part):
                m = comment_re.match(comment_part)
                if m:
                    prefix = m.group(1)
                    content = m.group(2)
                    translated = translate(content)
                    if translated != content:
                        new_lines[i] = before + prefix + translated
                        replacements += 1

    # ── Pass 2: translate string literals ──────────────────────────────────
    # Re-join after comment pass
    source2 = '\n'.join(new_lines)
    
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source2).readline))
    except tokenize.TokenError:
        tokens = []

    # Build replacement map: (start_offset, end_offset) -> new_string_token
    # We work on the string level
    replacements2 = []
    
    source_bytes = source2  # work with str
    
    for tok in tokens:
        if tok.type != tokenize.STRING:
            continue
        raw = tok.string
        if not CHINESE_RE.search(raw):
            continue
        
        # Detect quote style
        if raw.startswith('"""') or raw.startswith("'''"):
            triple = True
            quote = raw[:3]
            inner = raw[3:-3]
        elif raw.startswith('"') or raw.startswith("'"):
            triple = False
            quote = raw[0]
            inner = raw[1:-1]
        elif raw.startswith('f"""') or raw.startswith("f'''"):
            triple = True
            quote = raw[1:4]
            inner = raw[4:-3]
            quote = 'f' + quote
        elif raw.startswith("f'") or raw.startswith('f"'):
            triple = False
            quote = raw[:2]
            inner = raw[2:-1]
        else:
            continue
        
        # Translate inner content
        translated_inner = translate(inner)
        if translated_inner == inner:
            continue
        
        # Reconstruct token
        if triple:
            new_tok = quote + translated_inner + quote[-3:]  # closing triple quote
        else:
            new_tok = quote + translated_inner + quote[-1]
        
        # Store (row, col, original, replacement)
        replacements2.append((tok.start, tok.end, raw, new_tok))

    # Apply string replacements from bottom to top (to preserve positions)
    if replacements2:
        # Convert to line-based replacement
        lines3 = source2.split('\n')
        # Sort by row descending
        replacements2.sort(key=lambda x: (-x[0][0], -x[0][1]))
        
        for start, end, orig, new_tok in replacements2:
            row_s, col_s = start[0] - 1, start[1]
            row_e, col_e = end[0] - 1, end[1]
            
            if row_s == row_e:
                # Single line
                line = lines3[row_s]
                lines3[row_s] = line[:col_s] + new_tok + line[col_e:]
            else:
                # Multi-line string replacement
                first_line = lines3[row_s][:col_s] + new_tok.split('\n')[0]
                last_line = new_tok.split('\n')[-1] + lines3[row_e][col_e:]
                middle_lines = new_tok.split('\n')[1:-1]
                lines3[row_s:row_e+1] = [first_line] + middle_lines + [last_line]
            
            replacements += 1
        
        source2 = '\n'.join(lines3)

    if replacements > 0:
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(source2)
    
    return replacements


def main():
    total_files = 0
    total_replacements = 0
    
    for root, dirs, files in os.walk(BACKEND_ROOT):
        dirs[:] = [d for d in dirs if d not in ['__pycache__', '.git', 'venv', 'node_modules']]
        for fname in files:
            if not fname.endswith('.py'):
                continue
            if fname in SKIP_FILES:
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, BACKEND_ROOT)
            
            try:
                n = translate_file(fpath)
                if n > 0:
                    print(f"  ✓ {rel}: {n} replacements")
                    total_files += 1
                    total_replacements += n
                else:
                    pass  # no chinese or already translated
            except Exception as e:
                print(f"  ✗ ERROR in {rel}: {e}")
    
    print(f"\nDone! Modified {total_files} files with {total_replacements} total replacements.")


if __name__ == '__main__':
    main()
