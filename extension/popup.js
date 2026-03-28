// Configure your backend URL here (e.g., https://your-app.onrender.com/factcheck)
const BACKEND_URL = 'http://localhost:8000/factcheck';

document.addEventListener('DOMContentLoaded', async () => {
    const detectedTextDiv = document.getElementById('detected-text');
    const checkBtn = document.getElementById('check-btn');
    const loading = document.getElementById('loading');
    const resultDiv = document.getElementById('result');

    let extractedText = "";

    // 1. Ask content script for text
    try {
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (tab) {
            chrome.tabs.sendMessage(tab.id, { action: "extractText" }, (response) => {
                if (response && response.text) {
                    extractedText = response.text;
                    detectedTextDiv.innerText = extractedText;
                    checkBtn.disabled = false;
                } else {
                    detectedTextDiv.innerText = "No claim detected on this page.";
                }
            });
        }
    } catch (err) {
        detectedTextDiv.innerText = "Error communicating with tab: " + err.message;
    }

    // 2. Click handler for check button
    checkBtn.addEventListener('click', async () => {
        checkBtn.style.display = 'none';
        loading.style.display = 'block';
        resultDiv.style.display = 'none';

        try {
            // Call FastAPI (assuming local for now)
            const response = await fetch(BACKEND_URL, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ text: extractedText })
            });

            if (!response.ok) {
                throw new Error('Backend error: ' + response.statusText);
            }

            const data = await response.json();

            // Basic formatting of Markdown result
            resultDiv.innerHTML = formatMarkdown(data.verdict_md);
            resultDiv.style.display = 'block';
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
        .replace(/\n\n/g, '<br><br>')
        .replace(/\n/g, '<br>')
        .replace(/- (.*?)/g, '• $1');
}
