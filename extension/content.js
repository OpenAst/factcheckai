// content.js
// Listen for requests from the popup
let __factcheck_overlay_iframe = null;
let __factcheck_overlay_toolbar = null;
const OVERLAY_STATE_KEY = '__factcheck_overlay_state';

function getOverlayState() {
    try {
        return JSON.parse(window.localStorage.getItem(OVERLAY_STATE_KEY) || '{}');
    } catch (_) {
        return {};
    }
}

function saveOverlayState(state) {
    try {
        window.localStorage.setItem(OVERLAY_STATE_KEY, JSON.stringify(state));
    } catch (_) {}
}

function applyOverlayLayout() {
    if (!__factcheck_overlay_iframe || !__factcheck_overlay_toolbar) return;
    const state = { dock: 'right', minimized: false, ...getOverlayState() };
    const width = Math.min(430, Math.max(380, Math.floor(window.innerWidth * 0.32)));
    const height = Math.min(650, Math.max(500, window.innerHeight - 90));
    const sideProp = state.dock === 'left' ? 'left' : 'right';
    const otherSideProp = state.dock === 'left' ? 'right' : 'left';

    [__factcheck_overlay_iframe, __factcheck_overlay_toolbar].forEach(el => {
        el.style[sideProp] = '20px';
        el.style[otherSideProp] = '';
    });

    __factcheck_overlay_iframe.style.width = `${width}px`;
    __factcheck_overlay_iframe.style.height = `${height}px`;
    __factcheck_overlay_iframe.style.bottom = '20px';
    __factcheck_overlay_iframe.style.display = state.minimized ? 'none' : 'block';

    __factcheck_overlay_toolbar.style.bottom = state.minimized ? '20px' : `${20 + height + 8}px`;
}

function createOverlay() {
    if (__factcheck_overlay_iframe) return;

        const iframe = document.createElement('iframe');
        // Use srcdoc to avoid Chrome blocking extension pages inside iframes.
        // The iframe content is a lightweight placeholder UI that will be
        // bridged to the content script for full functionality later.
        iframe.srcdoc = `<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{margin:0;font-family:system-ui,Segoe UI,Helvetica,Arial;background:#f4f7f6;color:#2c3e50}
.panel{box-sizing:border-box;padding:12px;background:#fff;border-radius:8px;height:100%;display:flex;flex-direction:column}
.title{font-weight:700;margin-bottom:8px} .content{flex:1;display:flex;align-items:center;justify-content:center;color:#999}
</style></head><body><div class="panel"><div class="title">SRT Fact-Check AI</div><div class="content">Panel loaded — interaction via extension bridge</div></div>
<script>
    // Notify parent that iframe is ready
    try{parent.postMessage({factcheckIframeReady:true}, '*');}catch(e){}
    // Forward clicks to parent for demo purposes
    window.addEventListener('message', (e)=>{
        if(e?.data?.action === 'close'){
            try{parent.postMessage({factcheckIframeClosed:true}, '*');}catch(_){}
        }
    });
</script></body></html>`;
    iframe.id = 'factcheck-overlay-iframe';
    iframe.style.position = 'fixed';
    iframe.style.bottom = '20px';
    iframe.style.zIndex = 2147483647;
    iframe.style.border = '1px solid rgba(0,0,0,0.15)';
    iframe.style.borderRadius = '8px';
    iframe.style.boxShadow = '0 6px 20px rgba(0,0,0,0.25)';
    iframe.style.background = '#fff';

    // Toolbar lets reviewers keep the panel out of the way without reopening the extension menu.
    const toolbar = document.createElement('div');
    toolbar.id = 'factcheck-overlay-toolbar';
    toolbar.style.position = 'fixed';
    toolbar.style.zIndex = 2147483647;
    toolbar.style.display = 'flex';
    toolbar.style.gap = '6px';
    toolbar.style.alignItems = 'center';
    toolbar.style.padding = '6px';
    toolbar.style.borderRadius = '8px';
    toolbar.style.background = '#24313a';
    toolbar.style.boxShadow = '0 6px 18px rgba(0,0,0,0.18)';

    function makeButton(label) {
        const btn = document.createElement('button');
        btn.textContent = label;
        btn.style.padding = '6px 10px';
        btn.style.borderRadius = '6px';
        btn.style.border = '1px solid rgba(255,255,255,0.18)';
        btn.style.background = '#fff';
        btn.style.color = '#24313a';
        btn.style.cursor = 'pointer';
        btn.style.font = '12px system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';
        return btn;
    }

    const sideBtn = makeButton('Side');
    sideBtn.addEventListener('click', () => {
        const state = { dock: 'right', minimized: false, ...getOverlayState() };
        state.dock = state.dock === 'left' ? 'right' : 'left';
        saveOverlayState(state);
        applyOverlayLayout();
    });

    const minimizeBtn = makeButton('Minimize');
    minimizeBtn.addEventListener('click', () => {
        const state = { dock: 'right', minimized: false, ...getOverlayState() };
        state.minimized = !state.minimized;
        saveOverlayState(state);
        minimizeBtn.textContent = state.minimized ? 'Restore' : 'Minimize';
        applyOverlayLayout();
    });

    const closeBtn = makeButton('Close');
    closeBtn.addEventListener('click', () => removeOverlay());

    toolbar.appendChild(sideBtn);
    toolbar.appendChild(minimizeBtn);
    toolbar.appendChild(closeBtn);

    document.documentElement.appendChild(iframe);
    document.documentElement.appendChild(toolbar);

    __factcheck_overlay_iframe = iframe;
    __factcheck_overlay_toolbar = toolbar;
    applyOverlayLayout();
    window.addEventListener('resize', applyOverlayLayout);
}

function removeOverlay() {
    if (__factcheck_overlay_iframe) {
        __factcheck_overlay_iframe.remove();
        __factcheck_overlay_iframe = null;
    }
    if (__factcheck_overlay_toolbar) {
        __factcheck_overlay_toolbar.remove();
        __factcheck_overlay_toolbar = null;
    }
    window.removeEventListener('resize', applyOverlayLayout);
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request && request.action) {
        if (request.action === 'extractText') {
            const text = extractSrtContent();
            sendResponse({ text: text });
            return true;
        }

        if (request.action === 'togglePin') {
            if (__factcheck_overlay_iframe) removeOverlay(); else createOverlay();
            sendResponse({ pinned: !!__factcheck_overlay_iframe });
            return true;
        }

        if (request.action === 'pin') {
            createOverlay();
            sendResponse({ pinned: true });
            return true;
        }

        if (request.action === 'unpin') {
            removeOverlay();
            sendResponse({ pinned: false });
            return true;
        }

        if (request.action === 'isPinned') {
            sendResponse({ pinned: !!__factcheck_overlay_iframe });
            return true;
        }
    }
    return true;
});

function extractSrtContent() {
    const allElements = Array.from(document.querySelectorAll('div, span, h1, h2, h3, h4, h5, h6'));

    function findByText(text) {
        const entry = allElements.find(el => {
            const inner = el.innerText.trim().toLowerCase();
            return inner === text.toLowerCase();
        });
        if (entry && entry.nextElementSibling) {
            return entry.nextElementSibling.innerText.trim();
        }
        return null;
    }

    // 1. Priority: "Content In Review"
    const inReview = findByText("Content In Review");
    if (inReview && inReview.length > 0) return inReview;

    // 2. Priority: "Transcript"
    const transcript = findByText("Transcript");
    if (transcript && transcript.length > 5) return transcript;

    // 3. Fallback: Any large text blocks (for internal tool custom layouts)
    const largeBlocks = allElements
        .filter(el => {
            // Only direct text-containing elements to avoid capturing the whole <body>
            if (el.children.length > 0) return false;
            const text = el.innerText.trim();
            return text.length > 50 && !text.includes("Detecting content");
        })
        .map(el => el.innerText.trim());

    if (largeBlocks.length > 0) {
        // Return the first significant block (usually the claim or the post body)
        return largeBlocks[0];
    }

    // 4. Fallback: Selected text or prompt
    return window.getSelection().toString().trim() || "No clear claim detected. Please select the text manually.";
}
