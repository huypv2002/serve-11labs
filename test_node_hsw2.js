/**
 * Test: Run hsw.js in Node.js with JSDOM + WebCrypto polyfill
 * Goal: Avoid browser overhead, solve HSW in pure Node.js (~1-2s vs 10-15s)
 */
const { JSDOM } = require('jsdom');
const fs = require('fs');
const crypto = require('crypto');
const { performance } = require('perf_hooks');

const hswCode = fs.readFileSync('hsw_current.js', 'utf8');

// Get req token from command line or use test
const reqToken = process.argv[2];
if (!reqToken) {
    console.error('Usage: node test_node_hsw2.js <req_token>');
    process.exit(1);
}

async function solveHSW(req) {
    const t0 = performance.now();

    // Create JSDOM with full browser-like environment
    const dom = new JSDOM(`<!DOCTYPE html><html><head></head><body></body></html>`, {
        url: 'https://elevenlabs.io/',
        pretendToBeVisual: true,
        runScripts: 'dangerously',
        resources: 'usable',
        beforeParse(window) {
            // Polyfill crypto.subtle
            window.crypto = crypto.webcrypto || crypto;
            
            // Performance
            window.performance = performance;
            window.performance.getEntries = () => [];
            
            // Screen
            window.screen = { width: 1920, height: 1080, colorDepth: 24 };
            window.outerHeight = 1080;
            window.innerWidth = 1920;
            window.innerHeight = 1080;
            
            // Navigator extras
            window.navigator.userAgentData = {
                brands: [
                    { brand: "Chromium", version: "130" },
                    { brand: "Google Chrome", version: "130" },
                ],
                mobile: false,
                platform: "Windows"
            };
            window.navigator.connection = { effectiveType: '4g', rtt: 50 };
            window.navigator.mediaDevices = { enumerateDevices: async () => [] };
            
            // OfflineAudioContext mock
            window.OfflineAudioContext = class {
                constructor() { this.destination = {}; }
                createOscillator() { return { connect: () => {}, start: () => {}, frequency: { value: 0 } }; }
                createDynamicsCompressor() { return { connect: () => {}, threshold: {value:0}, knee: {value:0}, ratio: {value:0}, attack: {value:0}, release: {value:0} }; }
                startRendering() { return Promise.resolve({ getChannelData: () => new Float32Array(1) }); }
            };
            
            // CSS mock
            window.CSS = { supports: () => true };
            
            // HTMLIFrameElement
            window.HTMLIFrameElement = class {};
            
            // NavigatorUAData
            window.NavigatorUAData = class {};
        }
    });

    const window = dom.window;

    // Inject hsw.js
    const scriptEl = window.document.createElement('script');
    scriptEl.textContent = hswCode;
    window.document.head.appendChild(scriptEl);

    // Wait a bit for script to initialize
    await new Promise(r => setTimeout(r, 100));

    if (typeof window.hsw !== 'function') {
        console.error('hsw not defined! Available globals:', Object.keys(window).filter(k => k.startsWith('h')));
        dom.window.close();
        return null;
    }

    console.log(`[setup] ${(performance.now() - t0).toFixed(0)}ms`);

    // Execute HSW
    const t1 = performance.now();
    try {
        const result = await window.hsw(req);
        console.log(`[solve] ${(performance.now() - t1).toFixed(0)}ms`);
        console.log(`[total] ${(performance.now() - t0).toFixed(0)}ms`);
        console.log(`[result] ${result ? result.slice(0, 50) + '...' : 'null'}`);
        dom.window.close();
        return result;
    } catch (e) {
        console.error(`[error] ${e.message}`);
        dom.window.close();
        return null;
    }
}

solveHSW(reqToken).then(() => process.exit(0)).catch(e => {
    console.error(e);
    process.exit(1);
});
