/**
 * Single-worker HSW solver (one process per worker)
 * Patchright headless + channel='chrome'
 * 
 * Protocol: JSON lines over stdin/stdout
 * Commands: init {proxy}, solve {hsw_js, req_token}, quit
 */
const { chromium } = require('patchright');
const readline = require('readline');

let browser = null;
let context = null;

async function init(proxyServer) {
    if (browser) {
        try { await browser.close(); } catch(e) {}
    }

    const opts = {
        headless: true,
        channel: 'chrome',
        args: ['--no-sandbox', '--disable-setuid-sandbox'],
    };
    if (proxyServer) {
        opts.proxy = { server: proxyServer };
    }

    browser = await chromium.launch(opts);
    context = await browser.newContext({
        viewport: { width: 1920, height: 1080 },
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    });
}

async function solve(hswJs, reqToken) {
    const page = await context.newPage();
    try {
        await page.route('https://elevenlabs.io/hsw', r => r.fulfill({
            status: 200, contentType: 'text/html',
            body: '<html><head></head><body></body></html>'
        }));
        await page.goto('https://elevenlabs.io/hsw', { waitUntil: 'domcontentloaded', timeout: 5000 });

        // Inject
        let ok = false;
        try {
            await page.addScriptTag({ content: hswJs });
            await new Promise(r => setTimeout(r, 50));
            ok = await page.evaluate('typeof hsw === "function"');
        } catch(e) {}
        if (!ok) {
            await page.evaluate((code) => {
                const s = document.createElement('script');
                s.textContent = code;
                document.head.appendChild(s);
            }, hswJs);
            await new Promise(r => setTimeout(r, 50));
            ok = await page.evaluate('typeof hsw === "function"');
        }
        if (!ok) throw new Error('hsw not available');

        return await page.evaluate((req) => hsw(req), reqToken);
    } finally {
        await page.close();
    }
}

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', async (line) => {
    try {
        const msg = JSON.parse(line);
        if (msg.cmd === 'init') {
            await init(msg.proxy || null);
            console.log(JSON.stringify({ ok: true }));
        } else if (msg.cmd === 'solve') {
            const t0 = Date.now();
            const token = await solve(msg.hsw_js, msg.req_token);
            console.log(JSON.stringify({ ok: true, token, ms: Date.now() - t0 }));
        } else if (msg.cmd === 'quit') {
            if (browser) await browser.close();
            process.exit(0);
        }
    } catch (e) {
        console.log(JSON.stringify({ ok: false, error: e.message }));
    }
});

console.log(JSON.stringify({ ready: true }));
