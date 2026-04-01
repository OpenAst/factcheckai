// content.js
// Listen for requests from the popup
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === "extractText") {
        const text = extractSrtContent();
        sendResponse({ text: text });
    }
    return true;
});

function extractSrtContent() {
    const allDivs = Array.from(document.querySelectorAll('div'));

    // 1. Target "Content In Review" body
    const contentHeader = allDivs.find(el => el.innerText.trim() === "Content In Review");
    if (contentHeader && contentHeader.nextElementSibling) {
        const text = contentHeader.nextElementSibling.innerText.trim();
        if (text.length > 10) return text;
    }

    // 2. Target "Transcript" body
    const transcriptHeader = allDivs.find(el => el.innerText.trim() === "Transcript");
    if (transcriptHeader && transcriptHeader.nextElementSibling) {
        const text = transcriptHeader.nextElementSibling.innerText.trim();
        if (text.length > 10) return text;
    }

    // 3. Fallback: Post messages or articles
    const selectors = [
        '[data-testid="post_message"]',
        'article div[dir="auto"]',
        'textarea'
    ];

    for (const selector of selectors) {
        const element = document.querySelector(selector);
        if (element && element.innerText.trim().length > 15) {
            return element.innerText.trim();
        }
    }

    // 4. Fallback: Current selection or prompt
    return window.getSelection().toString().trim() || "No clear claim detected. Please select the text manually.";
}
