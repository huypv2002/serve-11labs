import base64, re

# Read admin HTML raw
with open('/Users/phamvanhuy/elevenlabs-re/tts-gateway/admin/index.html', 'r') as f:
    html = f.read()

# Base64 encode
html_b64 = base64.b64encode(html.encode('utf-8')).decode('ascii')

# Read current worker.js
with open('/Users/phamvanhuy/elevenlabs-re/tts-gateway/src/worker.js', 'r') as f:
    content = f.read()

# Strip line number prefixes if present
lines = content.split('\n')
clean_lines = []
for line in lines:
    m = re.match(r'^\s*\d+\|(.*)$', line)
    if m:
        clean_lines.append(m.group(1))
    else:
        clean_lines.append(line)

# Find where actual JS starts (const CORS_HEADERS)
js_start = None
for i, line in enumerate(clean_lines):
    if 'const CORS_HEADERS' in line:
        js_start = i
        break

if js_start is None:
    print("ERROR: Could not find CORS_HEADERS")
    exit(1)

js_code = '\n'.join(clean_lines[js_start:])
print(f"JS code starts at line {js_start}, length: {len(js_code)}")

# Use TextDecoder for proper UTF-8 support
decoder_js = '''const ADMIN_HTML_B64 = "''' + html_b64 + '''";
const ADMIN_HTML = (function() {
  const bin = atob(ADMIN_HTML_B64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new TextDecoder().decode(bytes);
})();

'''

# Write final worker
final = decoder_js + js_code
with open('/Users/phamvanhuy/elevenlabs-re/tts-gateway/src/worker.js', 'w') as f:
    f.write(final)

print(f"Written worker.js: {len(final)} bytes")
