// Configure your backend URL here (e.g., https://your-app.onrender.com/factcheck)
const BACKEND_URL = 'https://factcheckai-api.onrender.com/factcheck';

document.addEventListener('DOMContentLoaded', async () => {
    const cacheBadge = document.getElementById('cache-badge');
    const copyBtn = document.getElementById('copy-btn');

    const resultDiv = document.getElementById('result');
    const retryBtn = document.getElementById('retry-btn');
    const checkBtn = document.getElementById('check-btn');
    const detectedTextDiv = document.getElementById('detected-text');
    const loading = document.getElementById('loading');
    const saveReviewStatus = document.getElementById('save-review-status');

    let extractedText = "";
    let currentFactCheckData = null;
    let currentSelectedClaim = "";

    function setMiniStatus(message, isError = false) {
        saveReviewStatus.style.display = 'block';
        saveReviewStatus.textContent = message;
        saveReviewStatus.style.background = isError ? '#fff1f0' : '#ecf9f6';
        saveReviewStatus.style.borderColor = isError ? '#f5c6cb' : '#cdeee6';
        saveReviewStatus.style.color = isError ? '#8a1f17' : '#2c3e50';
    }

    async function saveSelectedEvidence(link) {
        if (!currentFactCheckData) {
            setMiniStatus('No fact-check result is loaded yet.', true);
            return;
        }

        try {
            const reviewUrl = BACKEND_URL.replace(/\/factcheck\/?$/, '/reviews');
            const resp = await fetch(reviewUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    post_text: currentFactCheckData.post_text || detectedTextDiv.value,
                    extracted_claim: currentFactCheckData.extracted_claim || '',
                    claim_status: currentFactCheckData.claim_status || 'factual_claim',
                    verdict_md: currentFactCheckData.verdict_md || '',
                    selected_evidence_url: link.url,
                    selected_evidence_title: link.title || '',
                    selected_evidence_snippet: link.snippet || '',
                    evidence_links: currentFactCheckData.evidence_links || []
                })
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || 'Could not save selected evidence');
            setMiniStatus('Selected evidence saved to the review database.');
        } catch (err) {
            setMiniStatus(err.message || 'Could not save selected evidence.', true);
        }
    }

    async function tryExtract() {
        detectedTextDiv.value = "Detecting content...";
        checkBtn.disabled = true;
        currentSelectedClaim = "";

        try {
            const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

            // Execute extraction script directly in the tab
            const results = await chrome.scripting.executeScript({
                target: { tabId: tab.id },
                func: () => {
                    const allElements = Array.from(document.querySelectorAll('div, span, h1, h2, h3, h4, h5, h6'));
                    const normalize = (value) => (value || '').replace(/\s+/g, ' ').trim();

                    function findByText(text) {
                        const entry = allElements.find(el => {
                            const inner = normalize(el.innerText).toLowerCase();
                            return inner === text.toLowerCase();
                        });
                        if (entry && entry.nextElementSibling) {
                            return normalize(entry.nextElementSibling.innerText);
                        }
                        return null;
                    }

                    function findLabeledBlock(labels) {
                        for (const el of allElements) {
                            const text = normalize(el.innerText);
                            const lowered = text.toLowerCase();
                            if (!labels.some(label => lowered === label || lowered.startsWith(label + ':'))) {
                                continue;
                            }

                            const next = el.nextElementSibling ? normalize(el.nextElementSibling.innerText) : '';
                            if (next && next.length > 15) return next;

                            const parentText = el.parentElement ? normalize(el.parentElement.innerText) : '';
                            if (parentText && parentText.toLowerCase() !== lowered) {
                                const stripped = parentText.replace(new RegExp(`^${text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*:?\\s*`, 'i'), '').trim();
                                if (stripped.length > 15) return stripped;
                            }
                        }
                        return null;
                    }

                    function extractInlineLabeledText(labels) {
                        const bodyText = normalize(document.body.innerText);
                        for (const label of labels) {
                            const regex = new RegExp(`${label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*:?\\s*([\\s\\S]{20,500})`, 'i');
                            const match = bodyText.match(regex);
                            if (match && match[1]) {
                                return normalize(match[1].split(/(?:content in review|transcript|creation time|link information)/i)[0]);
                            }
                        }
                        return null;
                    }

                    const mediaText = findLabeledBlock(['all detected text', 'text in media']) || extractInlineLabeledText(['all detected text', 'text in media']);
                    if (mediaText && mediaText.length > 15) return mediaText;

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

    // 2. Click handler for check button
    checkBtn.addEventListener('click', async () => {
        checkBtn.style.display = 'none';
        loading.style.display = 'block';
        resultDiv.style.display = 'none';
        copyBtn.style.display = 'none';
        cacheBadge.style.display = 'none';
        currentFactCheckData = null;
        saveReviewStatus.style.display = 'none';

        const extractedClaimBox = document.getElementById('extracted-claim-box');
        const extractedClaimText = document.getElementById('extracted-claim-text');
        const evidenceSection = document.getElementById('evidence-section');
        const evidenceLinksDiv = document.getElementById('evidence-links');
        const copyLinksBtn = document.getElementById('copy-links-btn');

        // Hide previous evidence
        extractedClaimBox.style.display = 'none';
        evidenceSection.style.display = 'none';
        evidenceLinksDiv.innerHTML = '';
        extractedClaimText.innerHTML = '';

        try {
            // Read current text from textarea (user might have edited it)
            const textToAnalyze = detectedTextDiv.value;

            const response = await fetch(BACKEND_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    text: textToAnalyze,
                    selected_claim: currentSelectedClaim || undefined
                })
            });

            if (!response.ok) {
                let backendDetail = response.statusText || 'Unknown backend error';
                try {
                    const errorData = await response.json();
                    backendDetail = errorData.detail || errorData.message || JSON.stringify(errorData);
                } catch (_) {
                    try {
                        backendDetail = await response.text();
                    } catch (_) {}
                }
                throw new Error('Backend error: ' + backendDetail);
            }

            const data = await response.json();
            currentFactCheckData = {
                ...data,
                post_text: textToAnalyze
            };

            // Show cache badge if applicable
            if (data.is_cached) cacheBadge.style.display = 'inline-block';

            // Show extracted claim preview
            if ((data.extracted_claims && data.extracted_claims.length > 0) || (data.extracted_claim && data.extracted_claim !== textToAnalyze)) {
                const claimOptions = data.extracted_claims && data.extracted_claims.length > 0
                    ? data.extracted_claims
                    : [data.extracted_claim];
                extractedClaimText.innerHTML = "";
                claimOptions.forEach((claim) => {
                    const row = document.createElement('div');
                    row.style.cssText = 'margin-top:6px;';

                    const claimText = document.createElement('div');
                    claimText.textContent = claim;
                    claimText.style.cssText = 'margin-bottom:4px; color:#333;';

                    const chooseBtn = document.createElement('button');
                    chooseBtn.textContent = claim === data.extracted_claim ? 'Selected Claim' : 'Check This Claim';
                    chooseBtn.className = 'retry-btn';
                    chooseBtn.style.cssText = 'width:auto; padding:4px 8px; font-size:11px;';
                    chooseBtn.disabled = claim === data.extracted_claim;
                    chooseBtn.addEventListener('click', () => {
                        currentSelectedClaim = claim;
                        checkBtn.click();
                    });

                    row.appendChild(claimText);
                    row.appendChild(chooseBtn);
                    extractedClaimText.appendChild(row);
                });
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
                    const anchor = document.createElement('a');
                    anchor.href = link.url;
                    anchor.target = '_blank';
                    anchor.className = 'report-link';
                    anchor.style.cssText = 'font-weight:600; display:block; margin-bottom:3px;';
                    anchor.textContent = link.title;

                    const snippet = document.createElement('span');
                    snippet.style.cssText = 'font-size:11px; color:#555; display:block;';
                    snippet.textContent = link.snippet || '';

                    const saveBtn = document.createElement('button');
                    saveBtn.className = 'save-source-btn';
                    saveBtn.textContent = 'Save This Source';
                    saveBtn.addEventListener('click', () => saveSelectedEvidence(link));

                    item.appendChild(anchor);
                    item.appendChild(snippet);
                    item.appendChild(saveBtn);
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
