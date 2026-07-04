const DEFAULT_API_BASE_URL = 'https://factcheckai.oneclyq.com';
let API_BASE_URL = DEFAULT_API_BASE_URL;
let BACKEND_URL = `${API_BASE_URL}/factcheck`;

document.addEventListener('DOMContentLoaded', async () => {
    const isEmbedded = new URLSearchParams(window.location.search).get('embedded') === '1';
    if (isEmbedded) document.body.classList.add('embedded');
    const cacheBadge = document.getElementById('cache-badge');
    const copyBtn = document.getElementById('copy-btn');

    const resultDiv = document.getElementById('result');
    const retryBtn = document.getElementById('retry-btn');
    const pinBtn = document.getElementById('pin-btn');
    const checkBtn = document.getElementById('check-btn');
    const detectedTextDiv = document.getElementById('detected-text');
    const loading = document.getElementById('loading');
    const saveReviewStatus = document.getElementById('save-review-status');
    const signalSection = document.getElementById('signal-section');
    const signalBadges = document.getElementById('signal-badges');
    const decisionSection = document.getElementById('decision-section');

    const decisionButtons = document.getElementById('decision-buttons');
    const openAllLinksBtn = document.getElementById('open-all-links-btn');
    const copySourceVerdictBtn = document.getElementById('copy-source-verdict-btn');

    let extractedText = "";
    let currentFactCheckData = null;
    let currentSelectedClaim = "";
    let currentSelectedEvidence = null;
    let currentFinalDecision = "";
    let rejectedEvidenceUrls = new Set();

    function setMiniStatus(message, isError = false) {
        saveReviewStatus.style.display = 'block';
        saveReviewStatus.textContent = message;
        saveReviewStatus.style.background = isError ? '#fff1f0' : '#ecf9f6';
        saveReviewStatus.style.borderColor = isError ? '#f5c6cb' : '#cdeee6';
        saveReviewStatus.style.color = isError ? '#8a1f17' : '#2c3e50';
    }

    function formatError(err) {
        if (!err) return 'Unknown error';
        if (typeof err === 'string') return err;
        return err.message || String(err);
    }

    function getDomain(url) {
        try {
            return new URL(url).hostname.replace(/^www\./, '');
        } catch (_) {
            return 'Unknown source';
        }
    }

    function getSourceType(link) {
        const domain = getDomain(link.url).toLowerCase();
        const title = (link.title || '').toLowerCase();
        if (domain.endsWith('.gov') || domain.endsWith('.mil') || title.includes('official')) return 'Official';
        if (/(reuters|apnews|bbc|npr|pbs|afp|politico|bloomberg|guardian|cnn)\./.test(domain)) return 'News';
        if (/(factcheck|snopes|politifact|leadstories|africacheck|stopfake|voxcheck)/.test(domain)) return 'Fact-check';
        if (/(who\.int|un\.org|europa\.eu|worldbank\.org|imf\.org)/.test(domain)) return 'Institutional';
        return 'Source';
    }

    function inferTrustSignals(data) {
        const links = data.evidence_links || [];
        const verdictText = (data.verdict_md || '').toLowerCase();
        const directEvidenceCount = links.filter(link => {
            const type = getSourceType(link);
            return ['Official', 'Fact-check', 'Institutional', 'News'].includes(type);
        }).length;
        const hasOfficial = links.some(link => getSourceType(link) === 'Official');
        const hasFactCheck = links.some(link => getSourceType(link) === 'Fact-check');
        const uncertainty = /(unclear|not enough|insufficient|could not verify|no direct evidence|unverified|needs context)/i.test(verdictText);

        let confidence = 'Medium confidence';
        let className = 'badge-warn';
        if (directEvidenceCount >= 2 && !uncertainty) {
            confidence = 'High confidence';
            className = 'badge-good';
        } else if (links.length === 0 || uncertainty) {
            confidence = 'Needs manual review';
            className = 'badge-risk';
        }

        const signals = [{ label: confidence, className }];
        if (data.language_label && data.language_label !== 'Unknown') {
            const confidenceLabel = data.language_confidence ? ` (${data.language_confidence})` : '';
            signals.push({ label: `Detected: ${data.language_label}${confidenceLabel}`, className: 'badge-signal' });
        }
        signals.push({ label: `${links.length} evidence link${links.length === 1 ? '' : 's'}`, className: links.length ? 'badge-signal' : 'badge-risk' });
        if (hasOfficial) signals.push({ label: 'Official source found', className: 'badge-good' });
        if (hasFactCheck) signals.push({ label: 'Fact-check source found', className: 'badge-good' });
        if (!hasOfficial && !hasFactCheck && links.length) signals.push({ label: 'Review source quality', className: 'badge-warn' });
        if (data.evidence_strategy === 'refutation') signals.push({ label: 'Refutation-focused search', className: 'badge-warn' });
        if (data.is_cached) signals.push({ label: 'Cached result', className: 'badge-warn' });
        if (data.claim_status && data.claim_status !== 'factual_claim') signals.push({ label: data.claim_status.replace(/_/g, ' '), className: 'badge-risk' });
        return signals;
    }

    function showTrustSignals(data) {
        const signals = inferTrustSignals(data);
        signalBadges.innerHTML = '';
        signals.forEach(signal => {
            const badge = document.createElement('span');
            badge.className = `badge badge-signal ${signal.className}`;
            badge.textContent = signal.label;
            signalBadges.appendChild(badge);
        });
        signalSection.style.display = 'block';
    }

    function pickDefaultEvidence(data) {
        const links = (data && data.evidence_links) || [];
        return currentSelectedEvidence || links.find(link => !rejectedEvidenceUrls.has(link.url)) || links[0] || null;
    }

    function resetForNewPost() {
        const extractedClaimBox = document.getElementById('extracted-claim-box');
        const extractedClaimText = document.getElementById('extracted-claim-text');
        const evidenceSection = document.getElementById('evidence-section');
        const evidenceLinksDiv = document.getElementById('evidence-links');

        currentFactCheckData = null;
        currentSelectedClaim = "";
        currentSelectedEvidence = null;
        currentFinalDecision = "";
        rejectedEvidenceUrls = new Set();

        cacheBadge.style.display = 'none';
        resultDiv.style.display = 'none';
        resultDiv.innerHTML = '';
        copyBtn.style.display = 'none';
        loading.style.display = 'none';
        signalSection.style.display = 'none';
        decisionSection.style.display = 'none';
        saveReviewStatus.style.display = 'none';
        checkBtn.style.display = 'block';
        checkBtn.disabled = true;

        if (extractedClaimBox) extractedClaimBox.style.display = 'none';
        if (evidenceSection) evidenceSection.style.display = 'none';
        if (extractedClaimText) extractedClaimText.innerHTML = '';
        if (evidenceLinksDiv) evidenceLinksDiv.innerHTML = '';
        signalBadges.innerHTML = '';
        decisionButtons.innerHTML = '';
    }

    async function readErrorDetail(response, fallbackMessage = 'Unknown backend error') {
        const status = `HTTP ${response.status}${response.statusText ? ` ${response.statusText}` : ''}`;
        const contentType = response.headers.get('content-type') || '';
        let detail = '';

        try {
            if (contentType.includes('application/json')) {
                const errorData = await response.json();
                detail = errorData.detail || errorData.message || JSON.stringify(errorData);
            } else {
                detail = await response.text();
            }
        } catch (err) {
            detail = `${fallbackMessage}; could not read error body (${formatError(err)})`;
        }

        return `${status}: ${detail || fallbackMessage}`;
    }

    async function saveSelectedEvidence(link, finalDecision = currentFinalDecision) {
        if (!currentFactCheckData) {
            setMiniStatus('No fact-check result is loaded yet.', true);
            return;
        }
        if ((!link || !link.url) && !finalDecision) {
            setMiniStatus('Choose a best source before saving this review.', true);
            return;
        }

        try {
            if (link && link.url) currentSelectedEvidence = link;
            const reviewUrl = BACKEND_URL.replace(/\/factcheck\/?$/, '/reviews');
            const resp = await fetch(reviewUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    post_text: currentFactCheckData.post_text || detectedTextDiv.value,
                    extracted_claim: currentFactCheckData.extracted_claim || '',
                    claim_status: currentFactCheckData.claim_status || 'factual_claim',
                    verdict_md: currentFactCheckData.verdict_md || '',
                    selected_evidence_url: link?.url || '',
                    selected_evidence_title: link?.title || '',
                    selected_evidence_snippet: link?.snippet || '',
                    evidence_links: currentFactCheckData.evidence_links || [],
                    rater_decision: finalDecision || '',
                    notes: finalDecision ? `Final decision: ${finalDecision}` : ''
                })
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.detail || 'Could not save selected evidence');
            setMiniStatus(finalDecision ? `Saved final decision: ${finalDecision}.` : 'Selected evidence saved to the review database.');
        } catch (err) {
            setMiniStatus(err.message || 'Could not save selected evidence.', true);
        }
    }

    function renderClaimOptions(data, extractedClaimBox, extractedClaimText) {
        const byClaim = new Map();
        (data.claim_options || []).forEach(option => {
            if (option.claim) byClaim.set(option.claim, option.evidence_links || []);
        });
        (data.extracted_claims || []).forEach(claim => {
            if (claim && !byClaim.has(claim)) byClaim.set(claim, []);
        });
        if (data.extracted_claim && !byClaim.has(data.extracted_claim)) {
            byClaim.set(data.extracted_claim, []);
        }

        const claimEntries = Array.from(byClaim.entries()).filter(([claim]) => claim);
        if (!claimEntries.length) return;

        extractedClaimText.innerHTML = '';
        claimEntries.forEach(([claim, evidenceLinks]) => {
            const item = document.createElement('div');
            item.className = `claim-option${claim === data.extracted_claim ? ' active' : ''}`;

            const claimText = document.createElement('div');
            claimText.textContent = claim;
            claimText.style.cssText = 'margin-bottom:6px; color:#27343b; font-size:12px; line-height:1.35;';

            const preview = document.createElement('div');
            preview.className = 'hint-text';
            preview.style.marginBottom = '6px';
            const topLinks = (evidenceLinks || []).slice(0, 3);
            preview.textContent = topLinks.length
                ? topLinks.map(link => `${getSourceType(link)}: ${link.title || getDomain(link.url)}`).join(' | ')
                : 'No preview evidence returned for this claim yet.';

            const chooseBtn = document.createElement('button');
            chooseBtn.textContent = claim === data.extracted_claim ? 'Selected Claim' : 'Check This Claim';
            chooseBtn.className = 'retry-btn';
            chooseBtn.style.cssText = 'width:auto; padding:4px 8px; font-size:11px;';
            chooseBtn.disabled = claim === data.extracted_claim;
            chooseBtn.addEventListener('click', () => {
                currentSelectedClaim = claim;
                checkBtn.click();
            });

            item.appendChild(claimText);
            item.appendChild(preview);
            item.appendChild(chooseBtn);
            extractedClaimText.appendChild(item);
        });
        extractedClaimBox.style.display = 'block';
    }

    function renderDecisionButtons() {
        const decisions = ['False', 'Misleading', 'True', 'Unverified', 'No Claim', 'Needs Review'];
        decisionButtons.innerHTML = '';
        decisions.forEach(decision => {
            const btn = document.createElement('button');
            btn.className = `decision-btn${decision === currentFinalDecision ? ' active' : ''}`;
            btn.textContent = decision;
            btn.addEventListener('click', async () => {
                currentFinalDecision = decision;
                Array.from(decisionButtons.children).forEach(child => child.classList.remove('active'));
                btn.classList.add('active');
                await saveSelectedEvidence(pickDefaultEvidence(currentFactCheckData), decision);
            });
            decisionButtons.appendChild(btn);
        });
        decisionSection.style.display = 'block';
    }

    function renderEvidenceLinks(data, evidenceLinksDiv) {
        const links = data.evidence_links || [];
        evidenceLinksDiv.innerHTML = '';
        if (!links.length) return;

        if (!currentSelectedEvidence) currentSelectedEvidence = links[0];

        links.forEach(link => {
            const item = document.createElement('div');
            item.className = 'evidence-item';
            if (currentSelectedEvidence && currentSelectedEvidence.url === link.url) item.classList.add('best');
            if (rejectedEvidenceUrls.has(link.url)) item.classList.add('rejected');

            const domain = document.createElement('span');
            domain.className = 'source-domain';
            domain.textContent = `${getSourceType(link)} | ${getDomain(link.url)}`;

            const anchor = document.createElement('a');
            anchor.href = link.url;
            anchor.target = '_blank';
            anchor.className = 'report-link';
            anchor.style.cssText = 'font-weight:600; display:block; margin-bottom:3px;';
            anchor.textContent = link.title || link.url;

            const snippet = document.createElement('span');
            snippet.style.cssText = 'font-size:11px; color:#555; display:block;';
            snippet.textContent = link.snippet || '';

            const bestBtn = document.createElement('button');
            bestBtn.className = 'secondary-btn';
            bestBtn.textContent = 'Best Source';
            bestBtn.addEventListener('click', () => {
                currentSelectedEvidence = link;
                renderEvidenceLinks(currentFactCheckData, evidenceLinksDiv);
                setMiniStatus('Best source selected. Final decision will save with this link.');
            });

            const rejectBtn = document.createElement('button');
            rejectBtn.className = 'secondary-btn';
            rejectBtn.textContent = rejectedEvidenceUrls.has(link.url) ? 'Restore Source' : 'Reject Source';
            rejectBtn.addEventListener('click', () => {
                if (rejectedEvidenceUrls.has(link.url)) {
                    rejectedEvidenceUrls.delete(link.url);
                } else {
                    rejectedEvidenceUrls.add(link.url);
                    if (currentSelectedEvidence && currentSelectedEvidence.url === link.url) {
                        currentSelectedEvidence = links.find(candidate => !rejectedEvidenceUrls.has(candidate.url) && candidate.url !== link.url) || null;
                    }
                }
                renderEvidenceLinks(currentFactCheckData, evidenceLinksDiv);
            });

            const saveBtn = document.createElement('button');
            saveBtn.className = 'save-source-btn';
            saveBtn.textContent = 'Save This Source';
            saveBtn.addEventListener('click', () => saveSelectedEvidence(link));

            item.appendChild(domain);
            item.appendChild(anchor);
            item.appendChild(snippet);
            item.appendChild(bestBtn);
            item.appendChild(rejectBtn);
            item.appendChild(saveBtn);
            evidenceLinksDiv.appendChild(item);
        });
    }

    async function tryExtract() {
        resetForNewPost();
        detectedTextDiv.value = "Detecting content...";

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

    async function sendPinMessage(tabId, action) {
        try {
            return await chrome.tabs.sendMessage(tabId, { action });
        } catch (_) {
            await chrome.scripting.executeScript({
                target: { tabId },
                files: ['content.js']
            });
            return chrome.tabs.sendMessage(tabId, { action });
        }
    }

    // Pin to page button
    if (pinBtn) {
        // Determine initial state; try content script first, then DOM fallback
        try {
            const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
            let state;
            try {
                state = await chrome.tabs.sendMessage(tab.id, { action: 'isPinned' });
            } catch (_) {
                state = undefined;
            }
            if (isEmbedded || (state && state.pinned)) {
                pinBtn.textContent = 'Unpin From Page';
            } else {
                try {
                    const check = await chrome.scripting.executeScript({
                        target: { tabId: tab.id },
                        func: () => !!document.getElementById('factcheck-overlay-root')
                    });
                    if (check && check[0] && check[0].result) pinBtn.textContent = 'Unpin From Page';
                } catch (_) {
                    // ignore
                }
            }
        } catch (e) {
            // ignore
        }

        pinBtn.addEventListener('click', async () => {
            try {
                const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
                const resp = await sendPinMessage(tab.id, isEmbedded ? 'unpin' : 'togglePin');
                if (resp && resp.pinned) {
                    pinBtn.textContent = 'Unpin From Page';
                    if (!isEmbedded) window.close();
                } else {
                    pinBtn.textContent = 'Pin To Page';
                }
            } catch (err) {
                setMiniStatus('Could not pin on this page: ' + (err.message || err), true);
            }
        });
    }

    // 2. Click handler for check button
    checkBtn.addEventListener('click', async () => {
        checkBtn.style.display = 'none';
        loading.style.display = 'block';
        loading.firstChild.textContent = 'Searching evidence and analyzing claim';
        resultDiv.style.display = 'none';
        copyBtn.style.display = 'none';
        cacheBadge.style.display = 'none';
        currentFactCheckData = null;
        currentSelectedEvidence = null;
        currentFinalDecision = "";
        rejectedEvidenceUrls = new Set();
        saveReviewStatus.style.display = 'none';

        const extractedClaimBox = document.getElementById('extracted-claim-box');
        const extractedClaimText = document.getElementById('extracted-claim-text');
        const evidenceSection = document.getElementById('evidence-section');
        const evidenceLinksDiv = document.getElementById('evidence-links');
        const copyLinksBtn = document.getElementById('copy-links-btn');

        // Hide previous evidence
        extractedClaimBox.style.display = 'none';
        evidenceSection.style.display = 'none';
        signalSection.style.display = 'none';
        decisionSection.style.display = 'none';
        evidenceLinksDiv.innerHTML = '';
        extractedClaimText.innerHTML = '';
        signalBadges.innerHTML = '';
        decisionButtons.innerHTML = '';

        try {
            // Read current text from textarea (user might have edited it)
            const textToAnalyze = detectedTextDiv.value;

            let response;
            try {
                response = await fetch(BACKEND_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        text: textToAnalyze,
                        selected_claim: currentSelectedClaim || undefined
                    })
                });
            } catch (err) {
                throw new Error(`Network error calling ${BACKEND_URL}: ${formatError(err)}`);
            }

            if (!response.ok) {
                throw new Error(`Backend error from ${BACKEND_URL}: ${await readErrorDetail(response)}`);
            }

            const data = await response.json();
            currentFactCheckData = {
                ...data,
                post_text: textToAnalyze
            };

            // Show cache badge if applicable
            if (data.is_cached) cacheBadge.style.display = 'inline-block';

            renderClaimOptions(data, extractedClaimBox, extractedClaimText);
            showTrustSignals(data);

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

            renderDecisionButtons();

            // Display evidence links
            if (data.evidence_links && data.evidence_links.length > 0) {
                const allUrls = data.evidence_links.map(l => l.url).join('\n');
                renderEvidenceLinks(data, evidenceLinksDiv);

                copyLinksBtn.onclick = () => {
                    navigator.clipboard.writeText(allUrls);
                    const orig = copyLinksBtn.innerText;
                    copyLinksBtn.innerText = "Copied!";
                    setTimeout(() => copyLinksBtn.innerText = orig, 2000);
                };

                openAllLinksBtn.onclick = () => {
                    data.evidence_links
                        .filter(link => !rejectedEvidenceUrls.has(link.url))
                        .slice(0, 5)
                        .forEach(link => chrome.tabs.create({ url: link.url, active: false }));
                };

                copySourceVerdictBtn.onclick = () => {
                    const best = pickDefaultEvidence(currentFactCheckData);
                    const text = [
                        `Claim: ${currentFactCheckData.extracted_claim || ''}`,
                        `Decision: ${currentFinalDecision || 'Not selected'}`,
                        `Best source: ${best ? `${best.title || best.url} - ${best.url}` : 'None selected'}`,
                        '',
                        currentFactCheckData.verdict_md || ''
                    ].join('\n');
                    navigator.clipboard.writeText(text);
                    const orig = copySourceVerdictBtn.innerText;
                    copySourceVerdictBtn.innerText = 'Copied!';
                    setTimeout(() => copySourceVerdictBtn.innerText = orig, 2000);
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
