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

    // 3. Fallback: Any large text blocks (for internal tool custom layouts)
    const largeBlocks = allElements
        .filter(el => {
            // Only direct text-containing elements to avoid capturing the whole <body>
            if (el.children.length > 0) return false;
            const text = el.innerText.trim();
            return text.length > 50 && !text.includes("Detecting content");
        })
        .map(el => el.innerText.trim());

    if (largeBlocks.length > 0) {
        // Return the first significant block (usually the claim or the post body)
        return largeBlocks[0];
    }

    // 4. Fallback: Selected text or prompt
    return window.getSelection().toString().trim() || "No clear claim detected. Please select the text manually.";
}
