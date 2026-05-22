/**
 * HSW solver using Patchright (anti-detection Chromium) - headless
 * Keeps browser alive, reuses pages for speed.
 * 
 * Patchright patches Chromium to bypass bot detection fingerprinting,
 * so hCaptcha tokens are accepted by ElevenLabs.
 * 
 * Protocol: JSON over stdin/stdout
 * Commands: init, solve, quit
 */
const { chromium } = require('patchright');
const readline = require('readline');

let browser = null;
let context = null;

async function init(proxyServer) {
    if (browser) {
        try { await browser.close(); } catch(e) {}
    }

    const launchOpts = {
        headless: true,
        channel: 'chrome',  // Use system Chrome (better fingerprint)
        args: [
            '--no-sandbox',
            '--disable-setuid-sandbox',
            '--disable-blink-features=AutomationControlled',
        ]
    };

    if (proxyServer) {
        launchOpts.proxy = { server: proxyServer };
    }

    browser = await chromium.launch(launchOpts);
    context = await browser.newContext({
        viewport: { width: 1920, height: 1080 },
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    });
}

async function solveHSW(hswJs, reqToken) {
    const page = await context.newPage();
    try {
        // Intercept navigation to serve blank page
        await page.route('https://elevenlabs.io/hsw', (route) => {
            route.fulfill({
                status: 200,
                contentType: 'text/html',
                body: '<html><head></head><body></body></html>'
            });
        });

        await page.goto('https://elevenlabs.io/hsw', { waitUntil: 'domcontentloaded', timeout: 5000 });

        // Inject hsw.js - try multiple methods
        let injected = false;

        // Method 1: addScriptTag
        try {
            await page.addScriptTag({ content: hswJs });
            await new Promise(r => setTimeout(r, 50));
            injected = await page.evaluate('typeof hsw === "function"');
        } catch(e) {}

        // Method 2: createElement
        if (!injected) {
            try {
                await page.evaluate((code) => {
                    const script = document.createElement('script');
                    script.textContent = code;
                    document.head.appendChild(script);
                }, hswJs);
                await new Promise(r => setTimeout(r, 50));
                injected = await page.evaluate('typeof hsw === "function"');
            } catch(e) {}
        }

        // Method 3: direct eval
        if (!injected) {
            await page.evaluate(hswJs);
            await new Promise(r => setTimeout(r, 50));
            injected = await page.evaluate('typeof hsw === "function"');
        }

        if (!injected) {
            throw new Error('hsw function not available after all methods');
        }

        // Execute proof-of-work
        const result = await page.evaluate((req) => hsw(req), reqToken);
        return result;
    } finally {
        await page.close();
    }
}

// JSON protocol over stdin/stdout
const rl = readline.createInterface({ input: process.stdin });

rl.on('line', async (line) => {
    try {
        const msg = JSON.parse(line);

        if (msg.cmd === 'init') {
            await init(msg.proxy || null);
            console.log(JSON.stringify({ ok: true }));
        } else if (msg.cmd === 'solve') {
            const t0 = Date.now();
            const result = await solveHSW(msg.hsw_js, msg.req_token);
            const elapsed = Date.now() - t0;
            console.log(JSON.stringify({ ok: true, token: result, ms: elapsed }));
        } else if (msg.cmd === 'quit') {
            if (browser) await browser.close();
            process.exit(0);
        } else {
            console.log(JSON.stringify({ ok: false, error: 'unknown cmd' }));
        }
    } catch (e) {
        console.log(JSON.stringify({ ok: false, error: e.message }));
    }
});

process.on('SIGTERM', async () => {
    if (browser) await browser.close();
    process.exit(0);
});

// Signal ready
console.log(JSON.stringify({ ready: true }));
