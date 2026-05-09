import os
import tokenize
import io
import re
import time
from deep_translator import GoogleTranslator

translator = GoogleTranslator(source='zh-CN', target='en')

def is_chinese(text):
    if not text: return False
    return bool(re.search(r'[\u4e00-\u9fa5]', text))

def translate_safe(text):
    if not is_chinese(text): return text
    try:
        # Split by lines to avoid huge requests and preserve formatting
        lines = text.splitlines()
        translated_lines = []
        for line in lines:
            if is_chinese(line):
                # Try to translate the whole line
                try:
                    # GoogleTranslator has a limit per request, usually 5000 chars
                    if len(line) > 4500:
                        translated_lines.append(line) # Skip too long lines
                    else:
                        trans = translator.translate(line)
                        translated_lines.append(trans)
                except Exception:
                    translated_lines.append(line)
            else:
                translated_lines.append(line)
        return "\n".join(translated_lines)
    except Exception as e:
        print(f"Error in translate_safe: {e}")
        return text

def translate_comment(comment_text):
    content = comment_text.lstrip('#').strip()
    if not is_chinese(content): return comment_text
    try:
        translated = translator.translate(content)
        return f"# {translated}"
    except:
        return comment_text

def translate_string(string_text):
    if not is_chinese(string_text): return string_text
    
    if string_text.startswith('"""') or string_text.startswith("'''"):
        quote = string_text[:3]
        content = string_text[3:-3]
        translated = translate_safe(content)
        return f"{quote}{translated}{quote}"
    else:
        # For single line strings, only translate if they look like human text (not code/ids)
        # But for now, let's just translate if it has Chinese.
        quote = string_text[0]
        content = string_text[1:-1]
        try:
            translated = translator.translate(content)
            return f"{quote}{translated}{quote}"
        except:
            return string_text

def process_file(filepath):
    print(f"Processing {filepath}")
    # We need to use binary mode for tokenize
    with open(filepath, 'rb') as f:
        try:
            tokens = list(tokenize.tokenize(f.readline))
        except Exception as e:
            print(f"Error tokenizing {filepath}: {e}")
            return

    # Read lines to modify them
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # Process tokens in reverse order (by start position) to avoid shifting issues
    # tokens are (type, string, start, end, line)
    # start and end are (row, col)
    
    # Filter only Chinese tokens
    chinese_tokens = [t for t in tokens if t.type in (tokenize.COMMENT, tokenize.STRING) and is_chinese(t.string)]
    
    # Sort reverse by start row/col
    chinese_tokens.sort(key=lambda x: x.start, reverse=True)

    for tok in chinese_tokens:
        s_row, s_col = tok.start
        e_row, e_col = tok.end
        
        if tok.type == tokenize.COMMENT:
            translated = translate_comment(tok.string)
            line = lines[s_row-1]
            lines[s_row-1] = line[:s_col] + translated + line[e_col:]
        elif tok.type == tokenize.STRING:
            # We translate triple-quoted docstrings and single-quoted strings with Chinese
            translated = translate_string(tok.string)
            if s_row == e_row:
                line = lines[s_row-1]
                lines[s_row-1] = line[:s_col] + translated + line[e_col:]
            else:
                # Multiline string replacement
                prefix = lines[s_row-1][:s_col]
                suffix = lines[e_row-1][e_col:]
                # Clear out the lines from s_row to e_row
                # We'll put the whole translated block in s_row
                for i in range(s_row, e_row + 1):
                    lines[i-1] = ""
                lines[s_row-1] = prefix + translated + suffix

    # Write back
    with open(filepath, 'w', encoding='utf-8') as f:
        for l in lines:
            if l != "": # We cleared some lines
                f.write(l)
    
    # Small sleep to be nice to the API
    time.sleep(0.5)

if __name__ == "__main__":
    base_path = "c:/Users/eduar/OneDrive/Desktop/homepty-swarm/backend"
    paths = ["app/services", "app/api", "scripts"]
    for p in paths:
        full_p = os.path.join(base_path, p)
        if not os.path.exists(full_p): continue
        for root, _, files in os.walk(full_p):
            for file in files:
                if file.endswith(".py") and file != "translate_backend.py":
                    process_file(os.path.join(root, file))
