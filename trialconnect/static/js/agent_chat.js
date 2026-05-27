/**
 * TrialConnect AI Chat Widget
 * Sends user messages + current search context to /api/agent_chat
 * The backend proxies to Vertex AI Agent Builder with injected trial context.
 */

let tcChatOpen = false;
let tcTrialContext = null;   // populated by index.html after search
let tcGreeted = false;

function tcToggleChat() {
    tcChatOpen = !tcChatOpen;
    const panel = document.getElementById('tc-chat-panel');
    panel.classList.toggle('open', tcChatOpen);
    if (tcChatOpen) {
        document.getElementById('tc-chat-badge').style.display = 'none';
        if (!tcGreeted) {
            tcGreeted = true;
            const hasResults = tcTrialContext && tcTrialContext.trials && tcTrialContext.trials.length > 0;
            const greeting = hasResults
                ? `Hi! 👋 I can see you searched for **${tcTrialContext.query}** near **${tcTrialContext.location}**. I have all **${tcTrialContext.trials.length} result(s)** in context, ready to help you.`
                : `Hi! 👋 I'm your TrialConnect AI assistant.\n\nTo get started, please search for trials using the bar above. Once you have results, come back here — I'll have your full context and can help you understand eligibility, compare trials, or explain medical terms.`;
            tcAddMessage('bot', greeting);
        }
        setTimeout(() => document.getElementById('tc-chat-input').focus(), 100);
    }
}

document.getElementById('tc-chat-btn').addEventListener('click', tcToggleChat);

document.getElementById('tc-chat-input').addEventListener('keydown', function(e) {
    if (e.key === 'Enter') tcSendMessage();
});

function tcAddMessage(role, text) {
    const container = document.getElementById('tc-chat-messages');
    const div = document.createElement('div');
    div.className = `tc-msg ${role}`;
    // Basic markdown: bold
    div.innerHTML = text.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>');
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return div;
}

async function tcSendMessage() {
    const input = document.getElementById('tc-chat-input');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';

    tcAddMessage('user', text);
    const typingEl = tcAddMessage('bot', 'Thinking…');
    typingEl.classList.add('typing');

    // Heuristic: update typing message based on likely agent action
    let thinkingMessage = 'Thinking…';
    if (text.toLowerCase().includes('search') || text.toLowerCase().includes('find')) {
        thinkingMessage = 'AI is searching for trials…';
    } else if (text.toLowerCase().includes('eligibility') || text.toLowerCase().includes('match')) {
        thinkingMessage = 'AI is checking eligibility…';
    } else if (tcTrialContext && tcTrialContext.trials && tcTrialContext.trials.length > 0) {
        thinkingMessage = 'AI is analyzing results…';
    }
    typingEl.textContent = thinkingMessage;

    try {
        const payload = {
            message: text,
            context: tcTrialContext || null
        };

        const resp = await fetch('/api/agent_chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const data = await resp.json();
        typingEl.remove();

        if (data.reply) {
            tcAddMessage('bot', data.reply);
        } else if (data.error) {
            tcAddMessage('bot', `Sorry, an error occurred: ${data.error}`);
        } else {
            tcAddMessage('bot', 'Sorry, I couldn\'t get a response from the AI agent. Please try again.');
        }
    } catch (err) {
        typingEl.remove();
        console.error("Agent chat connection error:", err);
        tcAddMessage('bot', 'Connection error with the AI agent. Please check your network and try again.');
    }
}

// Called by index.html after search results render:
// tcSetContext({ query: 'lung cancer', location: 'Chicago, IL', trials: [...] })
function tcSetContext(ctx) {
    tcTrialContext = ctx;
    // Show badge to hint the assistant is ready
    if (!tcChatOpen) {
        const badge = document.getElementById('tc-chat-badge');
        badge.style.display = 'flex';
        badge.textContent = ctx.trials ? ctx.trials.length : '!';
    }
    tcGreeted = false; // reset so greeting reflects new search
}
