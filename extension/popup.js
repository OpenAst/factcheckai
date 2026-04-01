// Configure your backend URL here (e.g., https://your-app.onrender.com/factcheck)
const BACKEND_URL = 'https://factcheckai-api.onrender.com/factcheck';

document.addEventListener('DOMContentLoaded', async () => {
    const cacheBadge = document.getElementById('cache-badge');
    const copyBtn = document.getElementById('copy-btn');

    let extractedText = "";

    // 1. Ask content script for text
    try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab && tab.url.includes("facebook.com")) {
            chrome.tabs.sendMessage(tab.id, { action: "extractText" }, (response) => {
                if (response && response.text) {
                    extractedText = response.text;
                    detectedTextDiv.value = extractedText;
                    checkBtn.disabled = false;
                } else {
                    detectedTextDiv.innerText = "No claim detected. Try selecting text manually.";
                    checkBtn.disabled = false; // Allow manual text selection if implemented
                }
            });
        } else {
            detectedTextDiv.value = "Please open a Facebook review page to use this tool.";
        }
    } catch (err) {
        detectedTextDiv.value = "Extension ready. Select text to fact-check.";
    }

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
