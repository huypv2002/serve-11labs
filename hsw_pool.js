/**
 * HSW solver - Multi-worker with proxy pool
 * Uses Patchright (anti-detection Chromium) headless + channel='chrome'
 * 
 * Protocol: JSON over stdin/stdout
 * Commands:
 *   init {proxies: ["socks5://ip:port", ...], workers: N}
 *   solve {worker_id: N, hsw_js: "...", req_token: "..."}
 *   quit
 */
const { chromium } = require('patchright');
const readline = require('readline');

const workers = new Map(); // worker_id -> {browser, context}

async function initWorker(id, proxyServer) {
    // Close existing
    if (workers.has(id)) {
        try { await workers.get(id).browser.close(); } catch(e) {}
    }

    const launchOpts = {
        headless: true,
        channel: 'chrome',
        args: ['--no-sandbox', '--disable-setuid-sandbox'],
    };

    if (proxyServer) {
        launchOpts.proxy = { server: proxyServer };
    }

    const browser = await chromium.launch(launchOpts);
    const context = await browser.newContext({
        viewport: { width: 1920, height: 1080 },
        userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    });

    workers.set(id, { browser, context });
}

async function solveHSW(workerId, hswJs, reqToken) {
    const w = workers.get(workerId);
    if (!w) throw new Error(`Worker ${workerId} not initialized`);

    const page = await w.context.newPage();
    try {
        await page.route('https://elevenlabs.io/hsw', (route) => {
            route.fulfill({
                status: 200,
                contentType: 'text/html',
                body: '<html><head></head><body></body></html>'
            });
        });

        await page.goto('https://elevenlabs.io/hsw', { waitUntil: 'domcontentloaded', timeout: 5000 });

        // Inject hsw.js
        let injected = false;
        try {
            await page.addScriptTag({ content: hswJs });
            await new Promise(r => setTimeout(r, 50));
            injected = await page.evaluate('typeof hsw === "function"');
        } catch(e) {}

        if (!injected) {
            try {
                await page.evaluate((code) => {
                    const s = document.createElement('script');
                    s.textContent = code;
                    document.head.appendChild(s);
                }, hswJs);
                await new Promise(r => setTimeout(r, 50));
                injected = await page.evaluate('typeof hsw === "function"');
            } catch(e) {}
        }

        if (!injected) {
            await page.evaluate(hswJs);
            await new Promise(r => setTimeout(r, 50));
            injected = await page.evaluate('typeof hsw === "function"');
        }

        if (!injected) throw new Error('hsw not available');

        const result = await page.evaluate((req) => hsw(req), reqToken);
        return result;
    } finally {
        await page.close();
    }
}

// JSON protocol
const rl = readline.createInterface({ input: process.stdin });

    rl.on('line', async (line) => {
    try {
        const msg = JSON.parse(line);

        if (msg.cmd === 'init') {
            const proxies = msg.proxies || [];
            const count = msg.workers || proxies.length;
            const promises = [];
            for (let i = 0; i < count; i++) {
                const proxy = proxies[i] || null;
                promises.push(initWorker(i, proxy));
            }
            await Promise.all(promises);
            console.log(JSON.stringify({ ok: true, workers: count }));
        } else if (msg.cmd === 'init_single') {
            // Reinit just one worker with new proxy
            await initWorker(msg.worker_id, msg.proxy || null);
            console.log(JSON.stringify({ ok: true }));
        } else if (msg.cmd === 'solve') {
            const t0 = Date.now();
            const result = await solveHSW(msg.worker_id, msg.hsw_js, msg.req_token);
            console.log(JSON.stringify({ ok: true, token: result, ms: Date.now() - t0 }));
        } else if (msg.cmd === 'quit') {
            for (const [id, w] of workers) {
                try { await w.browser.close(); } catch(e) {}
            }
            process.exit(0);
        } else {
            console.log(JSON.stringify({ ok: false, error: 'unknown cmd' }));
        }
    } catch (e) {
        console.log(JSON.stringify({ ok: false, error: e.message }));
    }
});

process.on('SIGTERM', async () => {
    for (const [id, w] of workers) {
        try { await w.browser.close(); } catch(e) {}
    }
    process.exit(0);
});

console.log(JSON.stringify({ ready: true }));
