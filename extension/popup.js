// Configure your backend URL here (e.g., https://your-app.onrender.com/factcheck)
const BACKEND_URL = 'https://factcheckai-api.onrender.com/factcheck';

document.addEventListener('DOMContentLoaded', async () => {
    const cacheBadge = document.getElementById('cache-badge');
    const copyBtn = document.getElementById('copy-btn');

    const resultDiv = document.getElementById('result');
    const retryBtn = document.getElementById('retry-btn');
    const checkBtn = document.getElementById('check-btn');
    const scanImagesBtn = document.getElementById('scan-images-btn');
    const detectedTextDiv = document.getElementById('detected-text');
    const loading = document.getElementById('loading');

    let extractedText = "";

    async function tryExtract() {
        detectedTextDiv.value = "Detecting content...";
        checkBtn.disabled = true;

        try {
            const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

            // Execute extraction script directly in the tab
            const results = await chrome.scripting.executeScript({
                target: { tabId: tab.id },
                func: () => {
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

                    // 3. Fallback: Any large text blocks
                    const largeBlocks = allElements
                        .filter(el => {
                            if (el.children.length > 0) return false;
                            const text = el.innerText.trim();
                            return text.length > 50 && !text.includes("Detecting content");
                        })
                        .map(el => el.innerText.trim());

                    if (largeBlocks.length > 0) return largeBlocks[0];

                    // 4. Fallback: Selection
                    return window.getSelection().toString().trim();
                }
            });

            if (results && results[0] && results[0].result) {
                extractedText = results[0].result;
                detectedTextDiv.value = extractedText;
                checkBtn.disabled = false;
            } else {
                detectedTextDiv.value = "No clear claim detected. You can type or paste the claim here manually.";
                checkBtn.disabled = false;
            }
        } catch (err) {
            detectedTextDiv.value = "Detection failed. Please select or paste text manually.";
            checkBtn.disabled = false;
        }
    }

    // Initial extraction
    tryExtract();

    // Retry button listener
    retryBtn.addEventListener('click', tryExtract);

    // Scan images for overlaid text using Tesseract.js (client-side OCR)
    if (scanImagesBtn) {
        scanImagesBtn.addEventListener('click', async () => {
            scanImagesBtn.disabled = true;
            detectedTextDiv.value = "Scanning images...";
            try {
                const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
                const results = await chrome.scripting.executeScript({
                    target: { tabId: tab.id },
                    func: () => {
                        const imgs = Array.from(document.images || []).filter(i => i.naturalWidth > 30 && i.naturalHeight > 10);
                        const out = [];
                        for (const img of imgs) {
                            try {
                                const canvas = document.createElement('canvas');
                                canvas.width = img.naturalWidth;
                                canvas.height = img.naturalHeight;
                                const ctx = canvas.getContext('2d');
                                ctx.drawImage(img, 0, 0);
                                out.push({ src: img.src, dataUrl: canvas.toDataURL('image/png') });
                            } catch (e) {
                                // CORS or other issues: fall back to src only
                                out.push({ src: img.src });
                            }
                        }
                        return out;
                    }
                });

                const images = (results && results[0] && results[0].result) || [];
                if (!images.length) {
                    detectedTextDiv.value = "No images found on this page.";
                    scanImagesBtn.disabled = false;
                    return;
                }

                let aggregated = "";
                for (let i = 0; i < images.length; i++) {
                    const img = images[i];
                    detectedTextDiv.value = `OCR image ${i+1}/${images.length}...`;
                    const src = img.dataUrl || img.src;
                    try {
                        const res = await Tesseract.recognize(src, 'eng');
                        const text = res && res.data && res.data.text ? res.data.text.trim() : '';
                        if (text.length > 0) aggregated += text + "\n\n";
                    } catch (ocrErr) {
                        console.warn('Tesseract OCR failed for image', img.src, ocrErr);
                    }
                }

                if (aggregated.trim().length > 0) {
                    detectedTextDiv.value = aggregated.trim();
                    checkBtn.disabled = false;
                } else {
                    detectedTextDiv.value = "No readable text found in images.";
                }

            } catch (err) {
                detectedTextDiv.value = "Image scanning failed: " + (err && err.message ? err.message : err);
            } finally {
                scanImagesBtn.disabled = false;
            }
        });
    }

    // 2. Click handler for check button
    checkBtn.addEventListener('click', async () => {
        checkBtn.style.display = 'none';
        loading.style.display = 'block';
        resultDiv.style.display = 'none';
        copyBtn.style.display = 'none';
        cacheBadge.style.display = 'none';

        const extractedClaimBox = document.getElementById('extracted-claim-box');
        const extractedClaimText = document.getElementById('extracted-claim-text');
        const evidenceSection = document.getElementById('evidence-section');
        const evidenceLinksDiv = document.getElementById('evidence-links');
        const copyLinksBtn = document.getElementById('copy-links-btn');

        // Hide previous evidence
        extractedClaimBox.style.display = 'none';
        evidenceSection.style.display = 'none';
        evidenceLinksDiv.innerHTML = '';

        try {
            // Read current text from textarea (user might have edited it)
            const textToAnalyze = detectedTextDiv.value;

            const response = await fetch(BACKEND_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text: textToAnalyze })
            });

            if (!response.ok) throw new Error('Backend error: ' + response.statusText);

            const data = await response.json();

            // Show cache badge if applicable
            if (data.is_cached) cacheBadge.style.display = 'inline-block';

            // Show extracted claim preview
            if (data.extracted_claim && data.extracted_claim !== textToAnalyze) {
                extractedClaimText.innerText = data.extracted_claim;
                extractedClaimBox.style.display = 'block';
            }

            // Show verdict and copy button
            resultDiv.innerHTML = formatMarkdown(data.verdict_md);
            resultDiv.style.display = 'block';
            copyBtn.style.display = 'block';

            // Copy report button
            copyBtn.onclick = () => {
                navigator.clipboard.writeText(data.verdict_md);
                const orig = copyBtn.innerText;
                copyBtn.innerText = "Copied!";
                setTimeout(() => copyBtn.innerText = orig, 2000);
            };

            // Render evidence links
            if (data.evidence_links && data.evidence_links.length > 0) {
                const allUrls = data.evidence_links.map(l => l.url).join('\n');

                data.evidence_links.forEach(link => {
                    const item = document.createElement('div');
                    item.style.cssText = 'margin-bottom:8px; padding:8px; background:#f5f5f5; border-radius:6px;';
                    item.innerHTML = `
                        <a href="${link.url}" target="_blank" class="report-link" style="font-weight:600; display:block; margin-bottom:3px;">${link.title}</a>
                        <span style="font-size:11px; color:#555;">${link.snippet}</span>
                    `;
                    evidenceLinksDiv.appendChild(item);
                });

                copyLinksBtn.onclick = () => {
                    navigator.clipboard.writeText(allUrls);
                    const orig = copyLinksBtn.innerText;
                    copyLinksBtn.innerText = "Copied!";
                    setTimeout(() => copyLinksBtn.innerText = orig, 2000);
                };

                evidenceSection.style.display = 'block';
            }

        } catch (err) {
            resultDiv.innerText = "Error: " + err.message;
            resultDiv.style.display = 'block';
            checkBtn.style.display = 'block';
        } finally {
            loading.style.display = 'none';
        }
    });
});

// Simple markdown subset formatter
function formatMarkdown(md) {
    return md
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.*?)\*/g, '<em>$1</em>')
        .replace(/\[(.*?)\]\((.*?)\)/g, '<a href="$2" target="_blank" class="report-link">$1</a>')
        .replace(/(https?:\/\/[^\s]+)/g, (url, p1, offset, string) => {
            // Avoid double-wrapping if already in a Markdown link
            const prevChar = string[offset - 1];
            if (prevChar === '(') return url;
            return `<a href="${url}" target="_blank" class="report-link">${url}</a>`;
        })
        .replace(/\n\n/g, '<br><br>')
        .replace(/\n/g, '<br>')
        .replace(/- (.*?)/g, '• $1');
}
