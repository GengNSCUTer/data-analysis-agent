import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { unsafeHTML } from 'lit/directives/unsafe-html.js';
import { vannaDesignTokens } from '../styles/vanna-design-tokens.js';
import { renderMarkdown } from './markdown-renderer.js';

@customElement('vanna-message')
export class VannaMessage extends LitElement {
  static styles = [
    vannaDesignTokens,
    css`
      :host {
        display: block;
        min-width: 0;
        max-width: 100%;
        padding: 0 var(--vanna-space-2);
        margin-bottom: var(--vanna-space-4);
        font-family: var(--vanna-font-family-default);
        animation: fade-in-up 0.25s ease-out;
      }

      :host(:last-of-type) {
        margin-bottom: 0;
      }

      @keyframes fade-in-up {
        from {
          opacity: 0;
          transform: translateY(16px);
        }
        to {
          opacity: 1;
          transform: translateY(0);
        }
      }

      .message {
        position: relative;
        min-width: 0;
        padding: var(--vanna-space-4) var(--vanna-space-5);
        border-radius: var(--vanna-chat-bubble-radius);
        word-wrap: break-word;
        line-height: 1.6;
        display: flex;
        flex-direction: column;
        gap: var(--vanna-space-2);
        max-width: min(85%, 580px);
        transition: transform var(--vanna-duration-200) ease, box-shadow var(--vanna-duration-200) ease;
        backdrop-filter: blur(8px);
      }

      .message.assistant {
        background: transparent;
        border: 0;
        color: var(--vanna-foreground-default);
        box-shadow: none;
        max-width: 100%;
        border-radius: 0;
        padding-left: 0;
        padding-right: 0;
      }

      .message.user {
        width: fit-content;
        max-width: min(78%, 420px);
        margin-left: auto;
        background: #eef3f6;
        color: #1c2430;
        box-shadow: none;
        border-radius: 8px;
        border: 1px solid #e4e7eb;
      }

      .message:hover {
        transform: none;
      }

      .message.assistant:hover {
        box-shadow: var(--vanna-shadow-md);
        border-color: var(--vanna-outline-hover);
      }

      .message.user:hover {
        box-shadow: var(--vanna-shadow-lg);
      }

      .message-content {
        margin: 0;
        min-width: 0;
        max-width: 100%;
        font-size: 15px;
        letter-spacing: 0.01em;
        white-space: pre-wrap;
        font-weight: 400;
      }

      .message-content.markdown {
        white-space: normal;
        overflow-wrap: anywhere;
      }

      .message-content.markdown :is(h1, h2, h3, h4, h5, h6) {
        margin: 0.2em 0 0.5em;
        line-height: 1.3;
      }

      .message-content.markdown p {
        margin: 0.35em 0;
      }

      .message-content.markdown :is(ul, ol) {
        margin: 0.35em 0;
        padding-left: 1.35em;
      }

      .message-content.markdown blockquote {
        margin: 0.5em 0;
        padding-left: 0.8em;
        border-left: 3px solid var(--vanna-accent-primary-default);
        color: var(--vanna-foreground-dimmer);
      }

      .message-content.markdown hr {
        border: 0;
        border-top: 1px solid var(--vanna-outline-dimmer);
        margin: 0.8em 0;
      }

      .message-content.markdown .text-markdown-table-wrap {
        max-width: 100%;
        overflow-x: auto;
        margin: 0.65em 0;
      }

      .message-content.markdown .text-markdown-table {
        width: max-content;
        min-width: 100%;
        border-collapse: collapse;
        font-size: 0.92em;
      }

      .message-content.markdown .text-markdown-table th,
      .message-content.markdown .text-markdown-table td {
        padding: 0.45em 0.65em;
        border: 1px solid var(--vanna-outline-dimmer);
        text-align: left;
        white-space: nowrap;
      }

      .message-content.markdown .text-markdown-table th {
        background: var(--vanna-background-higher);
        font-weight: 650;
      }

      .message-content.markdown .text-markdown-code {
        max-width: 100%;
        overflow-x: auto;
        padding: 0.7em;
        border-radius: var(--vanna-border-radius-sm);
        background: var(--vanna-background-higher);
        white-space: pre;
      }

      .message-content a {
        color: inherit;
        font-weight: 500;
        text-decoration: underline;
        text-decoration-thickness: 1px;
        text-underline-offset: 2px;
        opacity: 0.9;
      }

      .message-content code {
        font-family: var(--vanna-font-family-mono);
        background: var(--vanna-background-higher);
        padding: 2px 6px;
        border-radius: var(--vanna-border-radius-sm);
        font-size: 13px;
        border: 1px solid var(--vanna-outline-dimmer);
      }

      .message.user .message-content code {
        background: rgba(255, 255, 255, 0.2);
        border-color: rgba(255, 255, 255, 0.3);
      }

      .message-timestamp {
        display: inline-flex;
        align-items: center;
        gap: var(--vanna-space-1);
        font-size: 11px;
        letter-spacing: 0.05em;
        margin-top: var(--vanna-space-2);
        font-family: var(--vanna-font-family-default);
        opacity: 0.7;
        font-weight: 500;
      }

      .message-timestamp::before {
        content: '';
        width: 3px;
        height: 3px;
        border-radius: var(--vanna-border-radius-full);
        background: currentColor;
        opacity: 0.8;
      }

      .message.assistant .message-timestamp {
        align-self: flex-start;
        color: var(--vanna-foreground-dimmest);
      }

      .message.assistant .message-timestamp::before {
        background: var(--vanna-accent-primary-default);
      }

      .message.user .message-timestamp {
        align-self: flex-end;
        color: rgba(255, 255, 255, 0.8);
      }

      .message.user .message-timestamp::before {
        background: rgba(255, 255, 255, 0.8);
      }

      :host([theme="dark"]) .message.assistant {
        background: var(--vanna-background-higher);
        border: 1px solid var(--vanna-outline-default);
        color: var(--vanna-foreground-default);
        box-shadow: var(--vanna-shadow-md);
      }

      :host([theme="dark"]) .message.assistant .message-content code {
        background: var(--vanna-background-highest);
        border-color: var(--vanna-outline-default);
      }

      :host([theme="dark"]) .message.assistant .message-timestamp {
        color: var(--vanna-foreground-dimmest);
      }

      :host([theme="dark"]) .message.assistant .message-timestamp::before {
        background: var(--vanna-accent-primary-default);
      }

      :host([theme="dark"]) .message.user {
        background: linear-gradient(135deg, var(--vanna-accent-primary-stronger) 0%, var(--vanna-accent-primary-default) 100%);
        color: white;
        box-shadow: var(--vanna-shadow-lg);
      }

      :host([theme="dark"]) .message.user .message-content code {
        background: rgba(255, 255, 255, 0.15);
        border-color: rgba(255, 255, 255, 0.25);
      }

      :host([theme="dark"]) .message.user .message-timestamp {
        color: rgba(255, 255, 255, 0.8);
      }

      :host([theme="dark"])       .message.user .message-timestamp::before {
        background: rgba(255, 255, 255, 0.8);
      }

      .edit-question {
        align-self: flex-end;
        margin-top: 6px;
        padding: 0;
        border: 0;
        background: transparent;
        color: rgba(255, 255, 255, 0.92);
        cursor: pointer;
        font: inherit;
        font-size: 12px;
      }

      .edit-box {
        display: grid;
        gap: 8px;
        width: min(80%, 500px);
        margin-left: auto;
        padding: 10px;
        background: #ffffff;
        border: 1px solid #c5d0d6;
        border-radius: 8px;
        box-shadow: 0 8px 20px rgba(23, 48, 66, 0.12);
      }

      .edit-box textarea {
        width: 100%;
        min-height: 72px;
        padding: 10px 12px;
        border: 1px solid #b7c4cb;
        border-radius: 6px;
        background: #f7fafb;
        color: #17212b;
        caret-color: #17212b;
        font: inherit;
        resize: vertical;
      }

      .edit-actions {
        display: flex;
        justify-content: flex-end;
        gap: 8px;
      }

      .edit-actions button {
        min-height: 28px;
        padding: 0 10px;
        border-radius: 6px;
        border: 1px solid #b7c4cb;
        background: #ffffff;
        color: #173042;
        cursor: pointer;
        font: inherit;
        font-size: 12px;
      }

      .edit-actions button:last-child {
        border-color: #0f766e;
        background: #0f766e;
        color: #ffffff;
      }

      @media (max-width: 600px) {
        .message {
          max-width: 100%;
        }

        .message.user {
          width: fit-content;
          max-width: 88%;
        }
      }
    `
  ];

  @property() content = '';
  @property() type: 'user' | 'assistant' = 'user';
  @property({ type: Boolean }) markdown = false;
  @property({ type: Number }) timestamp = Date.now();
  @property({ reflect: true }) theme = 'light';
  @property({ type: Boolean }) canEdit = false;
  @state() private editing = false;
  @state() private draft = '';

  private formatTimestamp(timestamp: number): string {
    return new Date(timestamp).toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit'
    });
  }

  private startEdit() {
    this.draft = this.content;
    this.editing = true;
  }

  private cancelEdit() {
    this.editing = false;
  }

  private submitEdit() {
    const content = this.draft.trim();
    if (!content || content === this.content.trim()) {
      this.editing = false;
      return;
    }
    this.editing = false;
    this.dispatchEvent(new CustomEvent('replace-question', {
      detail: { content },
      bubbles: true,
      composed: true,
    }));
  }

  render() {
    const isMarkdown = this.markdown && this.type === 'assistant';
    if (this.type === 'user' && this.editing) {
      return html`
        <div class="edit-box">
          <textarea .value=${this.draft} @input=${(event: Event) => {
            this.draft = (event.target as HTMLTextAreaElement).value;
          }}></textarea>
          <div class="edit-actions">
            <button type="button" @click=${this.cancelEdit}>取消</button>
            <button type="button" @click=${this.submitEdit}>发送</button>
          </div>
        </div>
      `;
    }
    return html`
      <div class="message ${this.type}">
        <div class="message-content ${isMarkdown ? 'markdown' : ''}">
          ${isMarkdown ? unsafeHTML(renderMarkdown(this.content)) : this.content}
        </div>
        <div class="message-timestamp">
          ${this.formatTimestamp(this.timestamp)}
        </div>
        ${this.type === 'user' && this.canEdit ? html`
          <button class="edit-question" type="button" @click=${this.startEdit}>编辑</button>
        ` : ''}
      </div>
    `;
  }
}
