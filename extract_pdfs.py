import sys
import re
import os

files = [
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_a107945f35ed\files\33fa1583.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_a23581ffe28e\files\560f3ba7.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_a23581ffe28e\files\e6e55822.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_a4f40d44370b\files\63d9a77d.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_c88cbfa337a5\files\36b96989.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_c88cbfa337a5\files\6eca8c4c.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_c9b34f89d142\files\0c375d3d.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_c9b34f89d142\files\bee360b9.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_cb2d1a3dacb8\files\5990e543.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_cb2d1a3dacb8\files\b036a3bc.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_cff9b083c3f6\files\70d0b1d4.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_d1acf659d9e7\files\32125c83.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_d1acf659d9e7\files\49ddbdd1.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_db0d28f9eb72\files\10290629.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_ea813c931e80\files\0f3d855c.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_ea813c931e80\files\8bd2c082.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_ef40ed67d01c\files\10947e9e.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_ef40ed67d01c\files\e3f4e4ee.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_ef9520629623\files\f57056b5.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_ef9520629623\files\fe54303f.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_f64de0a80628\files\0a1e452f.pdf',
    r'C:\Users\eduar\OneDrive\Desktop\homepty-swarm\backend\uploads\projects\proj_f64de0a80628\files\5b5223ae.pdf',
]

for f in files:
    try:
        with open(f, 'rb') as fp:
            data = fp.read()
        text = data.decode('latin-1', errors='replace')
        stem = os.path.basename(f)
        print(f'=== {stem} ===')
        title_match = re.findall(r'/Title\s*\(([^)]+)\)', text)
        author_match = re.findall(r'/Author\s*\(([^)]+)\)', text)
        subject_match = re.findall(r'/Subject\s*\(([^)]+)\)', text)
        keywords_match = re.findall(r'/Keywords\s*\(([^)]+)\)', text)
        print(f'Title: {title_match}')
        print(f'Author: {author_match}')
        print(f'Subject: {subject_match}')
        print(f'Keywords: {keywords_match}')
        strings = re.findall(r'\(([^)]{10,200})\)', text)
        readable = [s for s in strings if s.isprintable()]
        print(f'Sample strings: {readable[:15]}')
        print()
    except Exception as e:
        print(f'Error {stem}: {e}')
