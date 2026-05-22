const fs = require('fs');
const hswCode = fs.readFileSync('hsw_current.js', 'utf8');

// Check Worker usage patterns
const workerLines = [];
const lines = hswCode.split('\n');
for (let i = 0; i < lines.length; i++) {
    if (lines[i].includes('Worker')) workerLines.push(`L${i}: ${lines[i].slice(0, 150)}`);
}
console.log('Worker lines:', workerLines.length);
workerLines.slice(0, 5).forEach(l => console.log(l));

// Check WebAssembly
const wasmCount = (hswCode.match(/WebAssembly/g) || []).length;
console.log('\nWebAssembly refs:', wasmCount);

// Check export pattern
const exportMatch = hswCode.match(/(window|self|globalThis)\s*\.\s*hsw\s*=/g);
console.log('Export:', exportMatch);

// Check structure - is it an IIFE?
console.log('\nFirst 500 chars:\n', hswCode.slice(0, 500));
console.log('\n\nLast 300 chars:\n', hswCode.slice(-300));

// Check if it's minified (single line?)
console.log('\nTotal lines:', lines.length);
console.log('Max line length:', Math.max(...lines.map(l => l.length)));
