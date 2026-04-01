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
        if (text.length > 0) return text;
    }

    // 2. Target "Transcript" body (look for all text in siblings if it's a list)
    const transcriptHeader = allDivs.find(el => el.innerText.trim() === "Transcript");
    if (transcriptHeader && transcriptHeader.parentElement) {
        // Try to get all text from the container next to or below the header
        const container = transcriptHeader.nextElementSibling || transcriptHeader.parentElement;
        const text = container.innerText.trim();
        // Filter out the header name itself if it was included
        const cleanText = text.replace(/^Transcript\s+/i, '').trim();
        if (cleanText.length > 5) return cleanText;
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
