// content.js
// Listen for requests from the popup
let __factcheck_overlay_root = null;
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
    if (!__factcheck_overlay_root || !__factcheck_overlay_toolbar) return;
    const state = { dock: 'right', minimized: false, ...getOverlayState() };
    const width = Math.min(430, Math.max(380, Math.floor(window.innerWidth * 0.32)));
    const height = Math.min(650, Math.max(500, window.innerHeight - 90));
    const sideProp = state.dock === 'left' ? 'left' : 'right';
    const otherSideProp = state.dock === 'left' ? 'right' : 'left';

    [__factcheck_overlay_root, __factcheck_overlay_toolbar].forEach(el => {
        el.style[sideProp] = '20px';
        el.style[otherSideProp] = '';
    });

    __factcheck_overlay_root.style.width = `${width}px`;
    __factcheck_overlay_root.style.height = `${height}px`;
    __factcheck_overlay_root.style.bottom = '20px';
    __factcheck_overlay_root.style.display = state.minimized ? 'none' : 'flex';

    __factcheck_overlay_toolbar.style.bottom = state.minimized ? '20px' : `${20 + height + 8}px`;
}

function createOverlay() {
    if (__factcheck_overlay_root) return;

    const root = document.createElement('div');
    root.id = 'factcheck-overlay-root';
    root.style.position = 'fixed';
    root.style.right = '20px';
    root.style.bottom = '20px';
    root.style.zIndex = '2147483647';
    root.style.border = '1px solid rgba(0,0,0,0.15)';
    root.style.borderRadius = '12px';
    root.style.boxShadow = '0 10px 30px rgba(0,0,0,0.25)';
    root.style.background = '#fff';
    root.style.display = 'flex';
    root.style.flexDirection = 'column';
    root.style.width = '360px';
    root.style.height = '500px';
    root.style.overflow = 'hidden';
    root.style.fontFamily = 'system-ui,Segoe UI,Helvetica,Arial,sans-serif';

    const toolbar = document.createElement('div');
    toolbar.id = 'factcheck-overlay-toolbar';
    toolbar.style.position = 'fixed';
    toolbar.style.zIndex = '2147483647';
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
    closeBtn.addEventListener('click', removeOverlay);

    toolbar.appendChild(sideBtn);
    toolbar.appendChild(minimizeBtn);
    toolbar.appendChild(closeBtn);

    const iframe = document.createElement('iframe');
    iframe.src = chrome.runtime.getURL('popup.html?embedded=1');
    iframe.title = 'SRT Fact-Check AI pinned panel';
    iframe.style.width = '100%';
    iframe.style.height = '100%';
    iframe.style.border = '0';
    iframe.style.background = '#f4f7f6';

    root.appendChild(iframe);
    document.documentElement.appendChild(root);
    document.documentElement.appendChild(toolbar);

    __factcheck_overlay_root = root;
    __factcheck_overlay_toolbar = toolbar;
    applyOverlayLayout();
    window.addEventListener('resize', applyOverlayLayout);
}
function removeOverlay() {
    if (__factcheck_overlay_root) {
        __factcheck_overlay_root.remove();
        __factcheck_overlay_root = null;
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
            if (__factcheck_overlay_root) removeOverlay(); else createOverlay();
            sendResponse({ pinned: !!__factcheck_overlay_root });
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
            sendResponse({ pinned: !!__factcheck_overlay_root });
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

    const inReview = findByText('Content In Review');
    if (inReview && inReview.length > 0) return inReview;

    const transcript = findByText('Transcript');
    if (transcript && transcript.length > 5) return transcript;

    const largeBlocks = allElements
        .filter(el => {
            if (el.children.length > 0) return false;
            const text = el.innerText.trim();
            return text.length > 50 && !text.includes('Detecting content');
        })
        .map(el => el.innerText.trim());

    if (largeBlocks.length > 0) return largeBlocks[0];

    return window.getSelection().toString().trim() || 'No clear claim detected. Please select the text manually.';
}
