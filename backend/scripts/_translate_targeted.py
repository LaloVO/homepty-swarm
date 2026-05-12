import os
import re
import time
from deep_translator import GoogleTranslator

translator_obj = GoogleTranslator(source='zh-CN', target='en')
CHINESE_RE = re.compile(r'[\u4e00-\u9fa5]')

def is_chinese(text):
    if not text: return False
    return bool(CHINESE_RE.search(text))

def translate_safe(text):
    if not is_chinese(text): return text
    try:
        lines = text.splitlines()
        translated_lines = []
        for line in lines:
            if is_chinese(line):
                try:
                    if len(line) > 4500:
                        translated_lines.append(line)
                    else:
                        trans = translator_obj.translate(line)
                        translated_lines.append(trans if trans else line)
                except Exception:
                    translated_lines.append(line)
            else:
                translated_lines.append(line)
        return "\n".join(translated_lines)
    except Exception as e:
        print(f"Error in translate_safe: {e}")
        return text

def translate_file(filepath, is_python=True):
    print(f"Processing {filepath}")
    with open(filepath, 'r', encoding='utf-8') as f:
        source = f.read()
    if not is_chinese(source):
        print("  No Chinese found.")
        return

    lines = source.split('\n')
    new_lines = []
    replacements = 0
    for i, line in enumerate(lines):
        if is_chinese(line):
            translated = translate_safe(line)
            if translated != line:
                new_lines.append(translated)
                replacements += 1
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)
    if replacements:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write('\n'.join(new_lines))
        print(f"  Translated {replacements} lines.")
    else:
        print("  No translations needed.")

files = [
    r"C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\scripts\run_parallel_simulation.py",
    r"C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\scripts\run_reddit_simulation.py",
    r"C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\scripts\run_twitter_simulation.py",
    r"C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\scripts\test_profile_format.py",
    r"C:\Users\eduar\OneDrive\Desktop\homepty-swarm\.env",
    r"C:\Users\eduar\OneDrive\Desktop\homepty-swarm\.gitignore",
    r"C:\Users\eduar\OneDrive\Desktop\homepty-swarm\Dockerfile",
]

for f in files:
    translate_file(f, is_python=f.endswith('.py'))
    time.sleep(0.3)

print("Done!")
