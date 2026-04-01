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

    // 2. Click handler for check button
    checkBtn.addEventListener('click', async () => {
        checkBtn.style.display = 'none';
        loading.style.display = 'block';
        resultDiv.style.display = 'none';
        copyBtn.style.display = 'none';
        cacheBadge.style.display = 'none';

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

            // Show result and copy button
            resultDiv.innerHTML = formatMarkdown(data.verdict_md);
            resultDiv.style.display = 'block';
            copyBtn.style.display = 'block';

            // Store raw markdown for copying
            copyBtn.onclick = () => {
                navigator.clipboard.writeText(data.verdict_md);
                const originalText = copyBtn.innerText;
                copyBtn.innerText = "Copied!";
                setTimeout(() => copyBtn.innerText = originalText, 2000);
            };

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
