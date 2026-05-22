import re, base64

# Read admin HTML
with open('admin/index.html') as f:
    html = f.read()

# Read worker code
with open('src/worker.js') as f:
    code = f.read()

# Remove old ADMIN_HTML constant
match = re.search(r'^const ADMIN_HTML = `.+?`;\s*', code, re.DOTALL | re.MULTILINE)
if match:
    code = code[match.end():]

# Also remove if it's base64 version
match = re.search(r'^const ADMIN_HTML_B64 = ".+?";\s*const ADMIN_HTML = atob\(ADMIN_HTML_B64\);\s*', code, re.DOTALL | re.MULTILINE)
if match:
    code = code[match.end():]

# Base64 encode HTML to avoid any escaping issues
html_b64 = base64.b64encode(html.encode()).decode()

# Write new file with base64 approach
with open('src/worker.js', 'w') as f:
    f.write(f'const ADMIN_HTML_B64 = "{html_b64}";\nconst ADMIN_HTML = atob(ADMIN_HTML_B64);\n\n{code}')

print('OK')
