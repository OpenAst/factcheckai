window.SRTCaptureTools = (() => {
    function initCaptureTools(options) {
        const {
            apiBaseUrl,
            isEmbedded,
            mediaBtn,
            listenBtn,
            detectedTextDiv,
            checkBtn,
            setMiniStatus,
            formatError,
            readErrorDetail,
            sendPinMessage
        } = options;

        const mediaAnalyzeUrl = `${apiBaseUrl}/media/analyze`;
        const audioTranscribeUrl = `${apiBaseUrl}/audio/transcribe`;
        let audioRecorder = null;
        let audioChunks = [];
        let audioStream = null;
        let audioContext = null;
        let audioAutoStopTimer = null;

        async function analyzeVisibleMedia() {
            mediaBtn.disabled = true;
            const originalText = mediaBtn.textContent;
            mediaBtn.textContent = 'Checking Media...';

            try {
                const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
                if (isEmbedded) {
                    await sendPinMessage(tab.id, 'hideForCapture');
                    await new Promise(resolve => setTimeout(resolve, 150));
                }

                let imageData;
                try {
                    imageData = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'png' });
                } finally {
                    if (isEmbedded) await sendPinMessage(tab.id, 'showAfterCapture');
                }

                const response = await fetch(mediaAnalyzeUrl, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        image_data: imageData,
                        post_text: detectedTextDiv.value
                    })
                });

                if (!response.ok) {
                    throw new Error(`Media analysis error: ${await readErrorDetail(response)}`);
                }

                const data = await response.json();
                const mediaBlock = [
                    'Media Analysis:',
                    data.media_text ? `Visible text: ${data.media_text}` : '',
                    data.media_claim ? `Image claim: ${data.media_claim}` : '',
                    data.search_terms ? `Image search terms: ${data.search_terms}` : '',
                    data.summary ? `Notes: ${data.summary}` : ''
                ].filter(Boolean).join('\n');

                const currentText = detectedTextDiv.value.trim();
                detectedTextDiv.value = currentText ? `${currentText}\n\n${mediaBlock}` : mediaBlock;
                checkBtn.disabled = false;
                setMiniStatus('Media analysis added. Run fact check when ready.');
            } catch (err) {
                setMiniStatus(`Could not check media: ${formatError(err)}`, true);
            } finally {
                mediaBtn.disabled = false;
                mediaBtn.textContent = originalText;
            }
        }

        function captureTabAudio() {
            return new Promise((resolve, reject) => {
                if (!chrome.tabCapture || !chrome.tabCapture.capture) {
                    reject(new Error('Tab audio capture is not available. Reload the extension after installing the updated version.'));
                    return;
                }
                chrome.tabCapture.capture({ audio: true, video: false }, stream => {
                    const lastError = chrome.runtime.lastError;
                    if (lastError) {
                        reject(new Error(lastError.message));
                        return;
                    }
                    if (!stream) {
                        reject(new Error('No audio stream was captured. Make sure the video is playing in this tab.'));
                        return;
                    }
                    resolve(stream);
                });
            });
        }

        function resetAudioCaptureUi() {
            clearTimeout(audioAutoStopTimer);
            audioAutoStopTimer = null;
            audioRecorder = null;
            audioChunks = [];
            if (audioStream) {
                audioStream.getTracks().forEach(track => track.stop());
                audioStream = null;
            }
            if (audioContext) {
                audioContext.close().catch(() => {});
                audioContext = null;
            }
            if (listenBtn) {
                listenBtn.disabled = false;
                listenBtn.textContent = 'Listen';
            }
        }

        async function startListening() {
            if (!listenBtn) return;
            listenBtn.disabled = true;
            listenBtn.textContent = 'Starting...';

            try {
                audioStream = await captureTabAudio();
                audioContext = new AudioContext();
                const source = audioContext.createMediaStreamSource(audioStream);
                source.connect(audioContext.destination);

                const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
                    ? 'audio/webm;codecs=opus'
                    : (MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : '');
                audioRecorder = mimeType
                    ? new MediaRecorder(audioStream, { mimeType })
                    : new MediaRecorder(audioStream);
                audioChunks = [];

                audioRecorder.ondataavailable = event => {
                    if (event.data && event.data.size > 0) audioChunks.push(event.data);
                };
                audioRecorder.onerror = event => {
                    setMiniStatus(`Audio capture failed: ${formatError(event.error || event)}`, true);
                    resetAudioCaptureUi();
                };
                audioRecorder.onstop = () => transcribeRecordedAudio();
                audioRecorder.start(1000);

                listenBtn.disabled = false;
                listenBtn.textContent = 'Stop Listening';
                setMiniStatus('Listening to this tab. Play the video, then click Stop Listening.');
                audioAutoStopTimer = setTimeout(() => {
                    if (audioRecorder && audioRecorder.state === 'recording') stopListening();
                }, 60000);
            } catch (err) {
                setMiniStatus(`Could not start listening: ${formatError(err)}`, true);
                resetAudioCaptureUi();
            }
        }

        function stopListening() {
            if (!audioRecorder || audioRecorder.state !== 'recording') {
                resetAudioCaptureUi();
                return;
            }
            clearTimeout(audioAutoStopTimer);
            audioAutoStopTimer = null;
            listenBtn.disabled = true;
            listenBtn.textContent = 'Transcribing...';
            audioRecorder.stop();
        }

        function blobToDataUrl(blob) {
            return new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onload = () => resolve(reader.result);
                reader.onerror = () => reject(reader.error || new Error('Could not read audio recording.'));
                reader.readAsDataURL(blob);
            });
        }

        async function transcribeRecordedAudio() {
            const chunks = audioChunks.slice();
            const mimeType = audioRecorder?.mimeType || 'audio/webm';
            if (audioStream) audioStream.getTracks().forEach(track => track.stop());
            audioStream = null;

            try {
                if (!chunks.length) {
                    throw new Error('No audio was recorded. Make sure the video is playing with sound.');
                }
                const blob = new Blob(chunks, { type: mimeType });
                if (blob.size < 1000) {
                    throw new Error('The recording was too short or silent.');
                }
                const audioData = await blobToDataUrl(blob);
                const response = await fetch(audioTranscribeUrl, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        audio_data: audioData,
                        post_text: detectedTextDiv.value
                    })
                });
                if (!response.ok) {
                    throw new Error(`Audio transcription error: ${await readErrorDetail(response)}`);
                }

                const data = await response.json();
                const transcript = (data.transcript || '').trim();
                if (!transcript) {
                    throw new Error(data.summary || 'No speech transcript was returned.');
                }

                const transcriptBlock = `Transcript:\n${transcript}`;
                const currentText = detectedTextDiv.value.trim();
                detectedTextDiv.value = currentText ? `${currentText}\n\n${transcriptBlock}` : transcriptBlock;
                checkBtn.disabled = false;
                setMiniStatus('Audio transcript added. Run fact check when ready.');
            } catch (err) {
                setMiniStatus(`Could not transcribe audio: ${formatError(err)}`, true);
            } finally {
                resetAudioCaptureUi();
            }
        }

        if (mediaBtn) mediaBtn.addEventListener('click', analyzeVisibleMedia);
        if (listenBtn) {
            listenBtn.addEventListener('click', () => {
                if (audioRecorder && audioRecorder.state === 'recording') {
                    stopListening();
                } else {
                    startListening();
                }
            });
        }
    }

    return { initCaptureTools };
})();
