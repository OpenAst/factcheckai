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
    let combinedText = "";

    // 1. Target "Content In Review" section
    const allDivs = Array.from(document.querySelectorAll('div'));
    const contentHeader = allDivs.find(el => el.innerText.trim() === "Content In Review");

    if (contentHeader) {
        // Find the next sibling or parent's child that contains the text
        const nextEl = contentHeader.nextElementSibling;
        if (nextEl) {
            combinedText += "--- CONTENT IN REVIEW ---\n" + nextEl.innerText.trim() + "\n\n";
        }
    }

    // 2. Target "Transcript" section
    const transcriptHeader = allDivs.find(el => el.innerText.trim() === "Transcript");
    if (transcriptHeader) {
        const nextEl = transcriptHeader.nextElementSibling;
        if (nextEl) {
            combinedText += "--- TRANSCRIPT ---\n" + nextEl.innerText.trim() + "\n\n";
        }
    }

    if (combinedText) return combinedText.trim();

    // Fallback: Try common selectors or selection
    const selectors = [
        '[data-testid="post_message"]',
        'article div[dir="auto"]',
        'textarea'
    ];

    for (const selector of selectors) {
        const element = document.querySelector(selector);
        if (element && element.innerText.trim()) {
            return element.innerText.trim();
        }
    }

    // Fallback: get selected text
    return window.getSelection().toString().trim() || "No text detected. Please select the text you want to fact-check.";
}
