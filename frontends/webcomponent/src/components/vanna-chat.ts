import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import { vannaDesignTokens } from '../styles/vanna-design-tokens.js';
import { VannaApiClient, ChatStreamChunk } from '../services/api-client.js';
import { ComponentManager, RichComponent } from './rich-component-system.js';
import './vanna-status-bar.js';
import './vanna-progress-tracker.js';
import './rich-card.js';
import './rich-task-list.js';
import './rich-progress-bar.js';
import './plotly-chart.js';

export interface ConversationHistoryMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp?: string;
}

export interface ConversationChartReplay {
  artifact_id?: string;
  title?: string;
  question?: string;
  data: unknown[];
  layout: Record<string, unknown>;
}

type ChatStatus = 'idle' | 'working' | 'error' | 'success' | 'warning';

@customElement('vanna-chat')
export class VannaChat extends LitElement {
  static styles = [
    vannaDesignTokens,
    css`
      *, *::before, *::after {
        box-sizing: border-box;
      }

      :host {
        container-type: inline-size;
        display: block;
        font-family: var(--vanna-font-family-default);
        --chat-primary: var(--vanna-accent-primary-default);
        --chat-primary-stronger: var(--vanna-accent-primary-stronger);
        --chat-primary-foreground: rgb(255, 255, 255);
        --chat-accent-soft: var(--vanna-accent-primary-subtle);
        --chat-outline: var(--vanna-outline-default);
        --chat-surface: var(--vanna-background-root);
        --chat-muted: var(--vanna-background-default);
        --chat-muted-stronger: var(--vanna-background-higher);
        max-width: 1024px;
        margin: 0 auto;
        background: var(--vanna-background-root);
        border: 1px solid var(--vanna-outline-dimmer);
        border-radius: var(--vanna-border-radius-2xl);
        box-shadow: var(--vanna-shadow-xl);
        overflow: hidden;
        transition: box-shadow var(--vanna-duration-300) ease, transform var(--vanna-duration-300) ease;
        position: relative;
      }

      :host(:hover) {
        box-shadow: var(--vanna-shadow-2xl);
        transform: translateY(-2px);
      }

      /* A host page may provide a resizable desktop window around the component.
       * Keep this opt-in so the standalone component retains its default sizing. */
      :host([data-hosted-window="true"]:not(.minimized):not(.maximized)) {
        width: 100%;
        height: 100%;
        min-height: 0;
        max-width: none;
        margin: 0;
        border: 0;
        border-radius: 0;
        box-shadow: none;
      }

      :host([data-hosted-window="true"]:not(.minimized):not(.maximized):hover) {
        box-shadow: none;
        transform: none;
      }

      :host([data-hosted-window="true"]) .sidebar {
        display: none;
      }

      :host([data-hosted-window="true"]) .chat-layout {
        grid-template-columns: minmax(0, 1fr);
      }


      :host([theme="dark"]) {
        --chat-primary: var(--vanna-accent-primary-default);
        --chat-primary-stronger: var(--vanna-accent-primary-stronger);
        --chat-primary-foreground: rgb(255, 255, 255);
        --chat-accent-soft: var(--vanna-accent-primary-subtle);
        --chat-outline: var(--vanna-outline-default);
        --chat-surface: var(--vanna-background-higher);
        --chat-muted: var(--vanna-background-default);
        --chat-muted-stronger: var(--vanna-background-highest);
        background: var(--vanna-background-higher);
        border-color: var(--vanna-outline-default);
      }

      :host(.maximized) {
        position: fixed;
        top: var(--vanna-space-6);
        left: var(--vanna-space-6);
        right: var(--vanna-space-6);
        bottom: var(--vanna-space-6);
        max-width: none;
        width: auto;
        margin: 0;
        z-index: var(--vanna-z-modal);
        border-radius: var(--vanna-border-radius-xl);
        transform: none;
        box-shadow: var(--vanna-shadow-2xl);
      }

      :host(.maximized):hover {
        transform: none;
      }

      :host(.minimized) {
        position: fixed !important;
        bottom: var(--vanna-space-6) !important;
        right: var(--vanna-space-6) !important;
        width: 64px !important;
        height: 64px !important;
        max-width: none !important;
        margin: 0 !important;
        z-index: var(--vanna-z-modal) !important;
        border-radius: var(--vanna-border-radius-full) !important;
        cursor: pointer !important;
        background: linear-gradient(135deg, var(--chat-primary-stronger), var(--chat-primary)) !important;
        border: 2px solid rgba(255, 255, 255, 0.9) !important;
        box-shadow: var(--vanna-shadow-xl) !important;
        overflow: hidden !important;
      }

      :host(.minimized):hover {
        transform: scale(1.05);
        box-shadow: var(--vanna-shadow-2xl) !important;
      }

      :host(.minimized) .chat-layout {
        display: none;
      }

      .minimized-icon {
        display: none;
      }

      :host(.minimized) .minimized-icon {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 100%;
        height: 100%;
        color: var(--chat-primary-foreground);
        font-size: 24px;
        transition: transform var(--vanna-duration-200) ease;
      }

      :host(.minimized) .minimized-icon:hover {
        transform: scale(1.1);
      }

      :host(.minimized) .minimized-icon svg {
        filter: drop-shadow(0 2px 4px rgba(0, 0, 0, 0.3));
      }

      .chat-layout {
        display: grid;
        grid-template-columns: minmax(0, 1fr) 300px;
        height: 600px;
        max-height: 80vh;
        background: var(--chat-muted);
        min-width: 0;
        min-height: 0;
      }

      :host([data-hosted-window="true"]:not(.minimized):not(.maximized)) .chat-layout {
        height: 100%;
        max-height: 100%;
        min-height: 0;
        grid-template-rows: minmax(0, 1fr);
      }

      :host(.maximized) .chat-layout {
        height: calc(100vh - 48px);
        max-height: calc(100vh - 48px);
      }

      .chat-layout.compact {
        grid-template-columns: 1fr;
      }

      .chat-main {
        display: flex;
        flex-direction: column;
        border-right: 1px solid var(--chat-outline);
        background: var(--chat-surface);
        min-width: 0;
        min-height: 0;
        overflow: hidden;
        container-type: size;
      }

      .chat-layout.compact .chat-main {
        border-right: none;
      }

      .chat-header {
        padding: var(--vanna-space-6) var(--vanna-space-7);
        background: #f7f8f7;
        border-bottom: 1px solid #e4e7eb;
        display: flex;
        flex-direction: column;
        gap: var(--vanna-space-2);
        color: #1c2430;
        position: relative;
        overflow: hidden;
      }

      .chat-header::before {
        content: '';
        position: absolute;
        top: -50%;
        right: -50%;
        width: 100%;
        height: 200%;
        background: radial-gradient(circle, rgba(255, 255, 255, 0.15) 0%, transparent 70%);
        opacity: 0.6;
        pointer-events: none;
      }

      :host([theme="dark"]) .chat-header {
        border-bottom-color: rgba(255, 255, 255, 0.1);
      }

      .header-top {
        position: relative;
        z-index: 1;
        display: flex;
        align-items: center;
        gap: var(--vanna-space-4);
        width: 100%;
      }

      .header-left {
        display: flex;
        align-items: center;
        gap: var(--vanna-space-4);
        min-width: 0;
        flex: 1;
      }

      .header-top-actions {
        display: inline-flex;
        align-items: center;
        gap: var(--vanna-space-2);
        margin-left: auto;
      }

      .chat-avatar {
        width: 28px;
        height: 28px;
        border-radius: 6px;
        background: #e7f4f3;
        display: grid;
        place-items: center;
        font-weight: 600;
        font-size: 12px;
        letter-spacing: 0;
        color: #0f766e;
        border: 1px solid #d5ebe8;
      }

      .header-text {
        display: flex;
        flex-direction: column;
        gap: var(--vanna-space-1);
        min-width: 0;
      }

      .chat-title {
        margin: 0;
        font-size: 15px;
        font-weight: 650;
        letter-spacing: 0;
        color: #1c2430;
      }

      .chat-subtitle {
        font-size: 13px;
        letter-spacing: 0.01em;
        opacity: 0.9;
        font-weight: 400;
      }

      :host([theme="dark"]) .chat-subtitle {
        opacity: 0.78;
      }

      .window-controls {
        display: inline-flex;
        gap: var(--vanna-space-2);
      }

      .window-control-btn {
        width: 32px;
        height: 32px;
        border-radius: var(--vanna-border-radius-lg);
        border: 1px solid #d5dbe2;
        background: #fff;
        color: #1c2430;
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        transition: all var(--vanna-duration-200) ease;
        backdrop-filter: blur(8px);
        position: relative;
        overflow: hidden;
      }

      .window-control-btn::before {
        content: '';
        position: absolute;
        inset: 0;
        background: linear-gradient(135deg, rgba(255, 255, 255, 0.2), transparent);
        opacity: 0;
        transition: opacity var(--vanna-duration-200) ease;
      }

      .window-control-btn:hover {
        transform: translateY(-1px) scale(1.05);
        background: rgba(255, 255, 255, 0.2);
        box-shadow: 
          0 8px 25px -8px rgba(0, 0, 0, 0.3),
          0 0 0 1px rgba(255, 255, 255, 0.2);
        border-color: rgba(255, 255, 255, 0.3);
      }

      .window-control-btn:hover::before {
        opacity: 1;
      }

      .window-control-btn:active {
        transform: translateY(0) scale(0.95);
      }

      .window-control-btn.minimize:hover {
        background: rgba(255, 193, 7, 0.2);
        color: #ffc107;
        box-shadow: 
          0 8px 25px -8px rgba(255, 193, 7, 0.4),
          0 0 0 1px rgba(255, 193, 7, 0.3);
      }

      .window-control-btn.maximize:hover,
      .window-control-btn.restore:hover {
        background: rgba(40, 167, 69, 0.2);
        color: #28a745;
        box-shadow: 
          0 8px 25px -8px rgba(40, 167, 69, 0.4),
          0 0 0 1px rgba(40, 167, 69, 0.3);
      }

      .window-control-btn svg {
        width: 16px;
        height: 16px;
        transition: transform var(--vanna-duration-150) ease;
      }

      .window-control-btn:hover svg {
        transform: scale(1.1);
      }

      :host([theme="dark"]) .window-control-btn {
        border-color: rgba(255, 255, 255, 0.1);
        background: rgba(255, 255, 255, 0.05);
      }

      :host([theme="dark"]) .window-control-btn:hover {
        background: rgba(255, 255, 255, 0.15);
        border-color: rgba(255, 255, 255, 0.25);
      }

      .chat-messages {
        flex: 1 1 0%;
        overflow-y: auto;
        overflow-x: hidden;
        padding: var(--vanna-space-6) var(--vanna-space-6) var(--vanna-space-5);
        background: linear-gradient(180deg, var(--chat-muted) 0%, var(--chat-surface) 70%);
        scroll-behavior: auto;
        overflow-anchor: none;
        display: block;
        min-height: 0;
        min-width: 0;
        max-height: 100%;
        position: relative;
        overscroll-behavior: contain;
      }

      .chat-messages::-webkit-scrollbar {
        width: 6px;
      }

      .chat-messages::-webkit-scrollbar-track {
        background: transparent;
      }

      .chat-messages::-webkit-scrollbar-thumb {
        background: var(--vanna-outline-default);
        border-radius: var(--vanna-border-radius-full);
        border: 1px solid var(--vanna-background-root);
      }

      .chat-messages::-webkit-scrollbar-thumb:hover {
        background: var(--vanna-outline-hover);
      }

      :host([theme="dark"]) .chat-messages {
        background: radial-gradient(circle at top, rgba(99, 102, 241, 0.12), transparent 55%), var(--chat-surface);
      }

      :host([theme="dark"]) .chat-messages::-webkit-scrollbar-thumb {
        background: var(--vanna-outline-default);
        border-color: var(--vanna-background-higher);
      }

      /* Scroll indicator when there's content above */
      .chat-messages::before {
        content: '';
        position: sticky;
        top: 0;
        display: block;
        height: 1px;
        background: linear-gradient(90deg, transparent, var(--vanna-accent-primary-default), transparent);
        opacity: 0;
        transition: opacity var(--vanna-duration-300) ease;
        z-index: 10;
        margin: 0 var(--vanna-space-4) var(--vanna-space-2);
      }

      .chat-messages.has-scroll::before {
        opacity: 0.5;
      }

      .rich-components-container {
        display: flex;
        flex-direction: column;
        gap: var(--vanna-space-4);
        width: 100%;
        max-width: 100%;
        min-width: 0;
      }

      .rich-components-container > * {
        min-width: 0;
        max-width: 100%;
      }

      .rich-component-wrapper {
        margin: var(--vanna-space-2) 0;
        animation: fade-in-up 0.3s ease-out;
      }

      .unknown-component {
        background: var(--vanna-background-higher);
        border: 1px solid var(--vanna-outline-default);
        border-radius: var(--vanna-border-radius-md);
        padding: var(--vanna-space-4);
        font-family: var(--vanna-font-family-mono);
        font-size: 12px;
      }

      .unknown-component p {
        margin: 0 0 var(--vanna-space-2) 0;
        color: var(--vanna-foreground-dimmer);
      }

      .unknown-component pre {
        margin: 0;
        color: var(--vanna-foreground-dimmest);
        overflow-x: auto;
      }

      .chat-input-area {
        padding: var(--vanna-space-5) var(--vanna-space-6) var(--vanna-space-6);
        background: var(--chat-surface);
        border-top: 1px solid var(--chat-outline);
        display: flex;
        flex-direction: column;
        gap: var(--vanna-space-4);
        flex: 0 1 auto;
        min-height: 0;
        min-width: 0;
      }

      /* A short embedded panel used to leave the message list only a few
       * pixels tall, so the wheel had nothing to scroll until the window was
       * maximized. Compact the composer whenever the panel itself is short. */
      @container (max-height: 560px) {
        .chat-header {
          padding: var(--vanna-space-3) var(--vanna-space-4);
        }

        .chat-messages {
          padding: var(--vanna-space-3) var(--vanna-space-4);
        }

        .chat-input-area {
          gap: var(--vanna-space-2);
          padding: var(--vanna-space-2) var(--vanna-space-3);
          max-height: 42%;
          overflow: auto;
        }

        .message-input {
          min-height: 36px;
          max-height: 72px;
          padding: 8px 0;
        }
      }

      /* The embedded host can be resized below the standalone component's
       * comfortable size. Keep the input/status area compact so the message
       * viewport never collapses to a few pixels. */
      @container (max-width: 420px) {
        .chat-header {
          padding: var(--vanna-space-4) var(--vanna-space-5);
        }

        .chat-input-area {
          gap: var(--vanna-space-2);
          padding: var(--vanna-space-3) var(--vanna-space-4) var(--vanna-space-4);
        }

        .chat-input-area > vanna-status-bar {
          max-height: 64px;
          margin-bottom: var(--vanna-space-1);
          overflow: hidden;
          padding: var(--vanna-space-2) var(--vanna-space-3);
        }
      }

      :host([theme="dark"]) .chat-input-area {
        border-top-color: rgba(148, 163, 184, 0.22);
      }

      .chat-input-container {
        display: flex;
        align-items: center;
        gap: var(--vanna-space-2);
        padding: 4px 8px 4px 12px;
        border-radius: 8px;
        background: var(--chat-muted);
        border: 1px solid var(--chat-muted-stronger);
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.6);
        transition: border-color var(--vanna-duration-200) ease, box-shadow var(--vanna-duration-200) ease, background var(--vanna-duration-200) ease;
      }

      .chat-input-container:focus-within {
        border-color: var(--chat-primary);
        box-shadow: 0 0 0 1px rgba(99, 102, 241, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.85);
        background: rgba(255, 255, 255, 0.95);
      }

      :host([theme="dark"]) .chat-input-container {
        background: rgba(15, 23, 42, 0.65);
        border-color: rgba(100, 116, 139, 0.45);
        box-shadow: inset 0 1px 0 rgba(148, 163, 184, 0.18);
      }

      :host([theme="dark"]) .chat-input-container:focus-within {
        border-color: rgba(129, 140, 248, 0.55);
        box-shadow: 0 0 0 1px rgba(129, 140, 248, 0.45), inset 0 1px 0 rgba(148, 163, 184, 0.25);
        background: rgba(30, 41, 59, 0.88);
      }

      .message-input {
        flex: 1;
        border: none;
        background: transparent;
        font-size: 15px;
        font-family: var(--vanna-font-family-default);
        line-height: 1.5;
        color: var(--vanna-foreground-default);
        resize: none;
        min-height: 48px;
        max-height: 140px;
        padding: 12px 0;
        outline: none;
      }

      :host([theme="dark"]) .message-input {
        color: rgba(226, 232, 240, 0.95);
      }

      .message-input::placeholder {
        color: rgba(71, 85, 105, 0.8);
      }

      :host([theme="dark"]) .message-input::placeholder {
        color: rgba(148, 163, 184, 0.65);
      }

      .message-input:focus {
        outline: none;
      }

      .message-input:disabled {
        color: rgba(148, 163, 184, 0.65);
        cursor: not-allowed;
      }

      :host([theme="dark"]) .message-input:disabled {
        color: rgba(100, 116, 139, 0.55);
      }

      .send-button {
        width: 48px;
        height: 48px;
        border-radius: 999px;
        border: none;
        background: linear-gradient(135deg, var(--chat-primary-stronger), var(--chat-primary));
        color: var(--chat-primary-foreground);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        transition: transform var(--vanna-duration-200) ease, box-shadow var(--vanna-duration-200) ease, filter var(--vanna-duration-200) ease;
        box-shadow: 0 18px 38px -24px rgba(79, 70, 229, 0.8);
      }

      .send-button:hover {
        transform: translateY(-1px) scale(1.02);
        box-shadow: 0 25px 45px -24px rgba(79, 70, 229, 0.85);
      }

      .send-button:active {
        transform: translateY(0) scale(0.98);
      }

      .send-button:disabled {
        background: rgba(148, 163, 184, 0.35);
        color: rgba(71, 85, 105, 0.7);
        cursor: not-allowed;
        transform: none;
        box-shadow: none;
      }

      .send-button svg {
        width: 18px;
        height: 18px;
      }

      .sidebar {
        background: linear-gradient(180deg, rgba(99, 102, 241, 0.08) 0%, rgba(15, 23, 42, 0.02) 100%);
        padding: var(--vanna-space-6);
        display: flex;
        flex-direction: column;
        gap: var(--vanna-space-4);
        overflow-y: auto;
        overflow-x: hidden;
        min-height: 0;
      }

      .sidebar::-webkit-scrollbar {
        width: 6px;
      }

      .sidebar::-webkit-scrollbar-track {
        background: transparent;
      }

      .sidebar::-webkit-scrollbar-thumb {
        background: var(--vanna-outline-default);
        border-radius: var(--vanna-border-radius-full);
      }

      :host([theme="dark"]) .sidebar {
        background: linear-gradient(180deg, rgba(79, 70, 229, 0.22) 0%, rgba(15, 23, 42, 0.45) 100%);
      }

      .empty-state {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        text-align: center;
        color: var(--vanna-foreground-dimmer);
        padding: var(--vanna-space-12) var(--vanna-space-8);
        margin: var(--vanna-space-8) var(--vanna-space-6);
        font-size: 15px;
        font-weight: 500;
        line-height: 1.6;
        background: linear-gradient(135deg, 
          rgba(255, 255, 255, 0.95) 0%, 
          rgba(248, 250, 252, 0.9) 50%,
          rgba(241, 245, 249, 0.85) 100%);
        border-radius: var(--vanna-border-radius-2xl);
        border: 2px dashed var(--vanna-accent-primary-default);
        box-shadow: 
          var(--vanna-shadow-sm),
          inset 0 1px 0 rgba(255, 255, 255, 0.8);
        backdrop-filter: blur(8px);
        transition: all var(--vanna-duration-300) ease;
      }

      .empty-state:hover {
        border-color: var(--vanna-accent-primary-stronger);
        transform: translateY(-2px);
        box-shadow: 
          var(--vanna-shadow-lg),
          inset 0 1px 0 rgba(255, 255, 255, 0.9);
      }

      :host([theme="dark"]) .empty-state {
        color: var(--vanna-foreground-dimmer);
        background: linear-gradient(135deg, 
          rgba(24, 29, 39, 0.95) 0%, 
          rgba(31, 39, 51, 0.9) 50%,
          rgba(17, 21, 28, 0.85) 100%);
        border-color: var(--vanna-accent-primary-default);
        box-shadow: 
          var(--vanna-shadow-md),
          inset 0 1px 0 rgba(129, 140, 248, 0.2);
      }

      :host([theme="dark"]) .empty-state:hover {
        border-color: var(--vanna-accent-primary-hover);
        box-shadow: 
          var(--vanna-shadow-xl),
          inset 0 1px 0 rgba(129, 140, 248, 0.3);
      }

      .empty-state-icon {
        width: 64px;
        height: 64px;
        margin: 0 auto var(--vanna-space-6);
        opacity: 0.7;
        color: var(--vanna-accent-primary-default);
        filter: drop-shadow(0 2px 4px rgba(79, 70, 229, 0.2));
      }

      .empty-state-text {
        font-size: 16px;
        font-weight: 600;
        color: var(--vanna-foreground-default);
        margin-bottom: var(--vanna-space-2);
      }

      .empty-state-subtitle {
        font-size: 14px;
        color: var(--vanna-foreground-dimmest);
        opacity: 0.8;
        font-weight: 400;
      }

      /* The component can be embedded in a narrow desktop-side panel. */
      @container (max-width: 880px) {
        .chat-layout {
          grid-template-columns: 1fr;
          height: min(600px, 85vh);
          max-height: 85vh;
        }

        :host([data-hosted-window="true"]:not(.minimized):not(.maximized)) .chat-layout {
          height: 100%;
          max-height: 100%;
        }

        .sidebar {
          display: none;
        }

        .chat-main {
          border-right: none;
        }
      }

      @container (max-width: 600px) {
        :host {
          border-radius: var(--vanna-border-radius-xl);
        }

        .chat-layout {
          height: min(500px, 80vh);
          max-height: 80vh;
        }

        :host([data-hosted-window="true"]:not(.minimized):not(.maximized)) .chat-layout {
          height: 100%;
          max-height: 100%;
        }

        .chat-header {
          border-bottom-width: 0;
          padding: var(--vanna-space-5) var(--vanna-space-5) var(--vanna-space-4);
        }

        .chat-messages {
          padding: var(--vanna-space-4) var(--vanna-space-4);
        }

        .empty-state {
          padding: var(--vanna-space-10) var(--vanna-space-6);
          margin: var(--vanna-space-6) var(--vanna-space-4);
          font-size: 14px;
        }

        .empty-state-text {
          font-size: 15px;
        }

        .empty-state-icon {
          width: 56px;
          height: 56px;
          margin-bottom: var(--vanna-space-5);
        }

        .chat-input-area {
          padding: var(--vanna-space-4) var(--vanna-space-4) var(--vanna-space-5);
        }
      }
    `
  ];

  @property() title = 'Vanna AI Chat';
  @property() placeholder = 'Ask me anything...';
  @property({ type: Boolean }) disabled = false;
  @state() private sendInFlight = false;
  @state() private stopping = false;
  private activeResponse: { request: any; controller: AbortController } | null = null;
  @property({ type: Boolean }) showProgress = true;
  @property({ type: Boolean }) allowMinimize = true;
  @property({ reflect: true }) theme = 'light';
  @property({ attribute: 'api-base' }) apiBaseUrl = '';
  @property({ attribute: 'sse-endpoint' }) sseEndpoint = '/api/vanna/v2/chat_sse';
  @property({ attribute: 'ws-endpoint' }) wsEndpoint = '/api/vanna/v2/chat_websocket';
  @property({ attribute: 'poll-endpoint' }) pollEndpoint = '/api/vanna/v2/chat_poll';
  @property() subtitle = '';
  @property({ attribute: 'empty-state-title' }) emptyStateTitle = 'Start a conversation';
  @property({ attribute: 'empty-state-subtitle' }) emptyStateSubtitle = 'Type your message below to begin chatting';
  @property({ attribute: 'minimize-title' }) minimizeTitle = 'Minimize';
  @property({ attribute: 'maximize-title' }) maximizeTitle = 'Maximize';
  @property({ attribute: 'restore-title' }) restoreTitle = 'Restore';
  @property({ attribute: 'send-button-label' }) sendButtonLabel = 'Send message';
  @property() startingState: 'normal' | 'maximized' | 'minimized' = 'normal';

  @state() private currentMessage = '';
  @state() private status: ChatStatus = 'idle';
  @state() private statusMessage = '';
  @state() private statusDetail = '';
  private _windowState: 'normal' | 'maximized' | 'minimized' = 'normal';

  @property({ reflect: false })
  get windowState() {
    return this._windowState;
  }

  set windowState(value: 'normal' | 'maximized' | 'minimized') {
    const oldValue = this._windowState;
    this._windowState = value;
    this.requestUpdate('windowState', oldValue);
  }

  private apiClient!: VannaApiClient;
  /** The host page may provide this value when it embeds the component. */
  @property({ attribute: 'conversation-id' }) conversationId = '';
  private componentManager: ComponentManager | null = null;
  private componentObserver: MutationObserver | null = null;

  constructor() {
    super();
    // Note: Don't create apiClient here - attributes haven't been set yet!
    // It will be created lazily in getApiClient() or firstUpdated()
    this.conversationId = this.generateId();
  }

  /**
   * Ensure API client is created/updated with current endpoint values
   */
  private ensureApiClient() {
    // Always recreate to ensure we have the latest endpoint values
    this.apiClient = new VannaApiClient({
      baseUrl: this.apiBaseUrl,
      sseEndpoint: this.sseEndpoint,
      wsEndpoint: this.wsEndpoint,
      pollEndpoint: this.pollEndpoint
    });
  }

  firstUpdated() {
    // Create API client now that attributes have been set
    this.ensureApiClient();

    // Initialize component manager with rich components container (fallback)
    const richContainer = this.shadowRoot?.querySelector('.rich-components-container') as HTMLElement;
    if (richContainer) {
      this.componentManager = new ComponentManager(richContainer);
      
      // Watch for changes in the rich components container to manage empty state
      this.componentObserver = new MutationObserver(() => {
        // Update empty state visibility
        this.updateEmptyState();
      });
      
      this.componentObserver.observe(richContainer, {
        childList: true,
        subtree: true,
        attributes: false
      });
    }

    // Set initial window state from startingState property
    if (this.startingState !== 'normal') {
      this._windowState = this.startingState;
    }

    // Set initial CSS class
    this.classList.add(this._windowState);

    const hostedConversationId = this.getAttribute('conversation-id')?.trim();
    if (hostedConversationId) {
      this.conversationId = hostedConversationId;
    }

    // The host page replays a saved transcript itself. A starter request here
    // would mint a fresh id and paint the welcome prompts over that history.
    if (!this.hasAttribute('data-defer-starter')) {
      this.requestStarterUI();
    }
  }

  /** Show example prompts only when the host has no transcript to replay. */
  showStarterIfEmpty(): void {
    const rich = this.shadowRoot?.querySelector('.rich-components-container');
    if (rich && rich.childElementCount > 0) return;
    void this.requestStarterUI();
  }

  /**
   * Request starter UI (buttons, welcome messages) from backend
   */
  private async requestStarterUI(): Promise<void> {
    try {
      const request = {
        message: "",
        conversation_id: this.conversationId,
        request_id: this.generateId(),
        metadata: {
          starter_ui_request: true
        }
      };

      // Stream the starter UI response
      await this.handleStreamingResponse(request);
    } catch (error) {
      console.error('Error requesting starter UI:', error);
      // Fail silently - starter UI is optional
    }
  }

  /** Set the server-side conversation used by subsequent requests. */
  setConversationId(conversationId: string): void {
    const normalized = String(conversationId || '').trim();
    if (!normalized || normalized === this.conversationId) return;
    this.conversationId = normalized;
    this.dispatchEvent(new CustomEvent('conversation-id-changed', {
      detail: { conversationId: normalized },
      bubbles: true,
      composed: true,
    }));
  }

  /** Replace the visible message stream with a safe server-side replay. */
  loadConversation(
    conversationId: string,
    messages: ConversationHistoryMessage[],
    charts?: ConversationChartReplay[],
  ): void {
    this.setConversationId(conversationId);
    this.clearMessages();
    const pending = (charts || []).slice();
    let armed: ConversationChartReplay | null = null;
    let replayed = 0;
    const finishArmed = () => {
      if (armed && this.appendReplayChart(armed)) replayed += 1;
      armed = null;
    };
    for (const message of messages) {
      if (message.role !== 'user' && message.role !== 'assistant') continue;
      if (!String(message.content || '').trim()) continue;
      if (message.role === 'user') finishArmed();
      this.addMessage(message.content, message.role, message.timestamp);
      if (message.role !== 'user') continue;
      const asked = this.normalizeChartQuestion(message.content);
      const index = pending.findIndex((chart) => this.normalizeChartQuestion(chart.question) === asked);
      if (index >= 0) armed = pending.splice(index, 1)[0];
    }
    finishArmed();
    replayed += pending.filter((chart) => this.appendReplayChart(chart)).length;
    this.setStatus('idle', '历史会话已恢复', replayed ? '已恢复保存的图表' : '');
    this.requestUpdate();
    if (replayed) this.scheduleChartIntoView();
    else this.scheduleLatestMessageScroll();
  }

  /** Start a fresh server-side conversation and request the starter UI. */
  startNewConversation(): string {
    void this.stopResponse();
    const conversationId = this.generateId();
    this.conversationId = conversationId;
    this.dispatchEvent(new CustomEvent('conversation-id-changed', {
      detail: { conversationId },
      bubbles: true,
      composed: true,
    }));
    this.clearMessages();
    this.setStatus('idle', '新会话已创建', '可以输入新的经营分析问题');
    void this.requestStarterUI();
    return conversationId;
  }

  connectedCallback() {
    super.connectedCallback();
    this.addEventListener('replace-question', this.onReplaceQuestion as EventListener);
  }

  disconnectedCallback() {
    this.activeResponse?.controller.abort();
    this.removeEventListener('replace-question', this.onReplaceQuestion as EventListener);
    super.disconnectedCallback();
    
    // Clean up mutation observer
    if (this.componentObserver) {
      this.componentObserver.disconnect();
      this.componentObserver = null;
    }
  }

  updated(changedProperties: Map<string, any>) {
    super.updated(changedProperties);

    // Update host classes based on window state
    if (changedProperties.has('windowState')) {
      this.classList.remove('normal', 'maximized', 'minimized');
      this.classList.add(this._windowState);
    }
    this.markLastUserMessageEditable();
  }

  private markLastUserMessageEditable() {
    const nodes = Array.from(this.shadowRoot?.querySelectorAll('vanna-message') || []) as unknown as Array<{
      type?: string;
      canEdit: boolean;
    }>;
    let lastUser: { canEdit: boolean } | null = null;
    for (const node of nodes) {
      if (node.type === 'user') lastUser = node;
    }
    for (const node of nodes) {
      node.canEdit = node === lastUser && !this.sendInFlight;
    }
  }

  private onReplaceQuestion = (event: Event) => {
    const content = (event as CustomEvent<{ content?: string }>).detail?.content || '';
    void this.replaceLastQuestion(content);
  };

  private async replaceLastQuestion(content: string) {
    const text = content.trim();
    if (!text || this.sendInFlight || !this.conversationId) return;
    const response = await fetch(
      `/api/project/conversations/${encodeURIComponent(this.conversationId)}/replace-last-question`,
      { method: 'POST', headers: this.getApiClient().getCustomHeaders() },
    );
    if (!response.ok) {
      this.setStatus('error', '无法替换上一轮提问', '请刷新后重试');
      return;
    }
    this.removeFromLastUserMessage();
    await this.sendMessage(text);
  }

  private removeFromLastUserMessage() {
    const components = this.componentManager?.getAllComponents() || [];
    let start = -1;
    components.forEach((component, index) => {
      if (component.type === 'user-message') start = index;
    });
    if (start < 0 || !this.componentManager) return;
    for (let index = components.length - 1; index >= start; index -= 1) {
      this.componentManager.processUpdate({
        operation: 'remove',
        target_id: components[index].id,
        timestamp: new Date().toISOString(),
      });
    }
  }

  private handleInput(e: Event) {
    const input = e.target as HTMLInputElement;
    this.currentMessage = input.value;
  }

  private handleKeyPress(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      this.sendMessage();
    }
  }

  /**
   * Send a message programmatically (can be called from buttons or external code)
   * Returns a Promise that resolves with success status
   */
  sendMessage(messageText?: string): Promise<boolean> {
    // Use provided message or fall back to current input
    // Check if messageText is actually a string (not an event object)
    const textToSend = (typeof messageText === 'string') ? messageText : this.currentMessage;

    if (!textToSend.trim() || this.disabled || this.sendInFlight) {
      return Promise.resolve(false);
    }

    return this._sendMessageInternal(textToSend);
  }

  private async _sendMessageInternal(messageText: string): Promise<boolean> {
    const hostDisabled = this.disabled;
    this.sendInFlight = true;
    this.disabled = true;
    try {
      return await this._sendMessageWhileLocked(messageText);
    } finally {
      this.sendInFlight = false;
      this.disabled = hostDisabled;
    }
  }

  private async _sendMessageWhileLocked(messageText: string): Promise<boolean> {
    // The embedded popup keeps the size the user chose. Auto-maximize only
    // applies to the standalone chat window.
    if (
      this.windowState !== 'maximized' &&
      this.windowState !== 'minimized' &&
      !this.hasAttribute('data-hosted-window')
    ) {
      this.maximizeWindow();
    }

    // Create user message as a rich component and send to ComponentManager
    const userRichComponent: RichComponent = {
      id: `user-message-${Date.now()}`,
      type: 'user-message',
      lifecycle: 'create',
      data: {
        content: messageText,
        sender: 'user'
      },
      children: [],
      timestamp: new Date().toISOString(),
      visible: true,
      interactive: false
    };

    // Add user message to ComponentManager for chronological ordering
    if (this.componentManager) {
      const update = {
        operation: 'create' as const,
        target_id: userRichComponent.id,
        component: userRichComponent,
        timestamp: userRichComponent.timestamp
      };
      this.componentManager.processUpdate(update);
    }

    // Update empty state after a brief delay to let ComponentManager render
    setTimeout(() => this.updateEmptyState(), 0);

    // Update the view
    this.requestUpdate();

    // Update status to working (initial frontend status before backend responds)
    this.setStatus('working', 'Sending message...', '');

    // Clear input only if we're sending from the input field
    if (messageText === this.currentMessage) {
      this.currentMessage = '';
      const input = this.shadowRoot?.querySelector('.message-input') as HTMLTextAreaElement;
      if (input) {
        input.value = '';
        input.style.height = 'auto';
      }
    }

    // Dispatch event for external listeners
    this.dispatchEvent(new CustomEvent('message-sent', {
      detail: { message: { content: messageText, type: 'user' } },
      bubbles: true,
      composed: true
    }));

    try {
      // Create the request
      const request = {
        message: messageText,
        conversation_id: this.conversationId,
        request_id: this.generateId(),
        metadata: {}
      };

      // Stream the response
      await this.handleStreamingResponse(request);
      return true; // Success

    } catch (error) {
      console.error('Error sending message:', error);
      this.setStatus('error', '本次发送未完成', '不会自动重新执行，请确认后重试');

      // Add error message
      this.addMessage(
        `本次请求未完成：${error instanceof Error ? error.message : '连接异常'}。系统没有自动重新执行，请稍后重试。`,
        'assistant'
      );
      return false; // Failure
    }
  }

  private getTitleInitials(): string {
    const title = (this.title || '').trim();
    if (!title) {
      return 'VA';
    }

    const parts = title.split(/\s+/).filter(Boolean);
    if (parts.length === 1) {
      return parts[0].charAt(0).toUpperCase() || 'V';
    }

    const first = parts[0].charAt(0);
    const last = parts[parts.length - 1].charAt(0);
    const initials = `${first}${last}`.toUpperCase();
    return initials || 'VA';
  }

  private minimizeWindow(e?: Event) {
    if (e) {
      e.stopPropagation();
      e.preventDefault();
    }
    this.windowState = 'minimized';
    this.dispatchEvent(new CustomEvent('window-state-changed', {
      detail: { state: 'minimized' },
      bubbles: true,
      composed: true
    }));
  }

  private maximizeWindow(e?: Event) {
    if (e) {
      e.stopPropagation();
      e.preventDefault();
    }
    this.windowState = 'maximized';
    this.dispatchEvent(new CustomEvent('window-state-changed', {
      detail: { state: 'maximized' },
      bubbles: true,
      composed: true
    }));
  }

  private restoreWindow(e?: Event) {
    if (e) {
      e.stopPropagation();
      e.preventDefault();
    }
    this.windowState = 'normal';
    this.dispatchEvent(new CustomEvent('window-state-changed', {
      detail: { state: 'normal' },
      bubbles: true,
      composed: true
    }));
  }


  addMessage(content: string, type: 'user' | 'assistant', timestamp?: string) {
    // Create message as a rich component and send to ComponentManager
    const richComponent: RichComponent = {
      id: `${type}-message-${this.generateId()}`,
      type: `${type}-message`,
      lifecycle: 'create',
      data: {
        content: content,
        sender: type
      },
      children: [],
      timestamp: timestamp || new Date().toISOString(),
      visible: true,
      interactive: false
    };

    if (this.componentManager) {
      const update = {
        operation: 'create' as const,
        target_id: richComponent.id,
        component: richComponent,
        timestamp: richComponent.timestamp
      };
      this.componentManager.processUpdate(update);
    }
  }

  private normalizeChartQuestion(value: unknown): string {
    return String(value || '').trim().replace(/[。．.！!？?\s]+$/g, '');
  }

  private appendReplayChart(chart: ConversationChartReplay): boolean {
    if (!this.componentManager || !Array.isArray(chart?.data) || !chart.data.length || !chart.layout) {
      return false;
    }
    const id = `history-chart-${chart.artifact_id || this.generateId()}`;
    const timestamp = new Date().toISOString();
    const component: RichComponent = {
      id,
      type: 'chart',
      lifecycle: 'create',
      data: {
        data: chart.data,
        layout: chart.layout,
        title: chart.title || '历史图表',
        config: { displayModeBar: false, responsive: true },
      },
      children: [],
      timestamp,
      visible: true,
      interactive: false,
    };
    this.componentManager.processUpdate({
      operation: 'create',
      target_id: id,
      component,
      timestamp,
    });
    return true;
  }

  setStatus(status: typeof this.status, message: string, detail?: string) {
    this.status = status;
    this.statusMessage = message;
    this.statusDetail = detail || '';
  }

  clearStatus() {
    this.statusMessage = '';
    this.statusDetail = '';
    this.status = 'idle';
  }

  getProgressTracker(): HTMLElement | null {
    return this.shadowRoot?.querySelector('vanna-progress-tracker') || null;
  }

  private async handleStreamingResponse(request: any) {
    // Ensure API client exists and is up to date
    if (!this.apiClient || this.apiClient.baseUrl !== this.apiBaseUrl) {
      this.ensureApiClient();
    }

    // Note: Status bar updates are now controlled by backend via StatusBarUpdateComponent
    // Frontend only shows initial "Sending message..." status (set in _sendMessageInternal)
    // and handles connection errors below

    const response = { request, controller: new AbortController() };
    this.activeResponse = response;
    try {
      // Use SSE streaming by default
      const stream = this.apiClient.streamChat(request, response.controller.signal);

      for await (const chunk of stream) {
        await this.processChunk(chunk);
      }

      this.settleCompletedStream(request);

    } catch (error) {
      if (response.controller.signal.aborted) {
        if (request.conversation_id === this.conversationId) this.setStatus('idle', '本次请求已停止', '已保存的结果仍可查看');
        return;
      }
      this.setStatus('error', '连接已中断', '本次不会自动重发，请确认后再试');
      throw error;
    } finally {
      if (this.activeResponse === response) this.activeResponse = null;
    }
  }

  async stopResponse(): Promise<void> {
    const response = this.activeResponse;
    if (!response || this.stopping) return;
    this.stopping = true;
    try {
      await this.apiClient.cancelChat(response.request);
    } catch (error) {
      console.warn('Cancel endpoint unavailable; closing the stream', error);
    } finally {
      response.controller.abort();
      this.stopping = false;
    }
  }

  private async processChunk(chunk: ChatStreamChunk) {
    // Ignore late chunks from a request that belongs to a conversation the
    // host has already replaced.
    if (chunk.conversation_id && chunk.conversation_id !== this.conversationId) {
      return;
    }

    if (chunk.conversation_id) {
      this.dispatchEvent(new CustomEvent('conversation-id-changed', {
        detail: { conversationId: chunk.conversation_id },
        bubbles: true,
        composed: true,
      }));
    }

    // Dispatch chunk event for external listeners
    this.dispatchEvent(new CustomEvent('chunk-received', {
      detail: { chunk },
      bubbles: true,
      composed: true
    }));

    this.syncStatusBarUpdate(chunk);

    // Handle rich components via ComponentManager
    if (chunk.rich && this.componentManager) {
      if (chunk.rich.id && chunk.rich.lifecycle) {
        // Standard rich component with lifecycle
        const component = chunk.rich as RichComponent;
        const update = {
          operation: chunk.rich.lifecycle as any,
          target_id: chunk.rich.id,
          component: component,
          timestamp: new Date().toISOString()
        };
        this.componentManager.processUpdate(update);
      } else if (chunk.rich.type === 'component_update') {
        // Component update format
        this.componentManager.processUpdate(chunk.rich as any);
      } else {
        // Generic rich component
        const component = chunk.rich as RichComponent;
        const update = {
          operation: 'create' as const,
          target_id: component.id || `component-${Date.now()}`,
          component: component,
          timestamp: new Date().toISOString()
        };
        this.componentManager.processUpdate(update);
      }
      if (chunk.rich.type === 'chart') this.scheduleChartIntoView();
      return;
    }

    // Update progress tracker for legacy components (keep for backward compatibility)
    const progressTracker = this.getProgressTracker();
    if (progressTracker && 'addStep' in progressTracker) {
      (progressTracker as any).addStep({
        id: `chunk-${Date.now()}`,
        title: this.getChunkTitle(chunk),
        status: 'completed',
        timestamp: chunk.timestamp
      });
    }

    // Handle different chunk types (legacy components)
    const componentType = chunk.rich?.type;
    switch (componentType) {
      case 'text':
        // Text chunks are handled in the main loop
        break;

      case 'thinking':
        // Legacy: Status bar updates now handled by backend via StatusBarUpdateComponent
        // This case is kept for backward compatibility but doesn't update status
        break;

      case 'tool_execution':
        // Legacy: Status bar updates now handled by backend via StatusBarUpdateComponent
        // This case is kept for backward compatibility but doesn't update status
        break;

      case 'error':
        throw new Error(chunk.rich.data?.message || 'Unknown error from agent');

      default:
        // Unknown legacy chunks are intentionally ignored; supported status
        // and progress updates arrive through the rich component pipeline.
        break;
    }
  }

  /**
   * Keep the component state in sync with server-owned UI updates. The rich
   * component manager applies these updates directly to the status element,
   * but its parent would otherwise retain the initial working state and could
   * restore stale loading text on the next Lit render.
   */
  private syncStatusBarUpdate(chunk: ChatStreamChunk): void {
    if (chunk.rich?.type !== 'status_bar_update') return;

    const data = chunk.rich.data || {};
    const status = data.status;
    if (!this.isChatStatus(status)) return;

    this.setStatus(
      status,
      typeof data.message === 'string' ? data.message : '',
      typeof data.detail === 'string' ? data.detail : '',
    );
  }

  /**
   * A normal SSE completion is authoritative even when a short-circuiting
   * server path only streams a result/status card and no status_bar_update.
   * Starter UI requests intentionally keep their server-provided idle state.
   */
  private settleCompletedStream(request: { message?: unknown }): void {
    if (!String(request.message || '').trim() || this.status !== 'working') return;
    this.clearStatus();
    if (this.shadowRoot?.querySelector('.rich-chart')) this.scheduleChartIntoView();
  }

  private isChatStatus(value: unknown): value is ChatStatus {
    return value === 'idle' || value === 'working' || value === 'error'
      || value === 'success' || value === 'warning';
  }


  private getChunkTitle(chunk: ChatStreamChunk): string {
    const componentType = chunk.rich?.type;
    switch (componentType) {
      case 'text':
        return 'Generating response';
      case 'thinking':
        return 'Thinking';
      case 'tool_execution':
        return `Tool: ${chunk.rich.data?.tool_name || 'Unknown'}`;
      default:
        return `Processing ${componentType || 'component'}`;
    }
  }

  private generateId(): string {
    return `${Date.now()}-${Math.random().toString(36).substring(2, 11)}`;
  }

  /**
   * Update the API base URL and recreate the client
   */
  updateApiBaseUrl(baseUrl: string) {
    this.apiBaseUrl = baseUrl;
    this.ensureApiClient();
  }

  /**
   * Get the API client instance for direct access
   */
  getApiClient(): VannaApiClient {
    if (!this.apiClient) {
      this.ensureApiClient();
    }
    return this.apiClient;
  }

  /**
   * Set custom headers for authentication or other purposes
   */
  setCustomHeaders(headers: Record<string, string>) {
    this.getApiClient().setCustomHeaders(headers);
  }

  /**
   * Update empty state visibility based on whether there are components
   */
  private updateEmptyState() {
    const emptyState = this.shadowRoot?.querySelector('#empty-state') as HTMLElement;
    const richContainer = this.shadowRoot?.querySelector('.rich-components-container') as HTMLElement;
    
    if (emptyState && richContainer) {
      // Show empty state if rich container has no children
      const hasContent = richContainer.children.length > 0;
      emptyState.style.display = hasContent ? 'none' : 'flex';
    }
  }

  /**
   * Update scroll indicator based on scroll position
   */
  private updateScrollIndicator() {
    const messagesContainer = this.shadowRoot?.querySelector('.chat-messages');
    if (!messagesContainer) return;
    
    // Check if there's content scrolled above
    const hasScrolledContent = messagesContainer.scrollTop > 10;
    
    // Update scroll indicator class
    messagesContainer.classList.toggle('has-scroll', hasScrolledContent);
  }

  /** Scroll to the end of the rendered conversation. */
  scrollToLastMessage() {
    const messagesContainer = this.shadowRoot?.querySelector('.chat-messages');
    const richContainer = this.shadowRoot?.querySelector('.rich-components-container');
    
    if (!messagesContainer || !richContainer) return;

    // Get the last child element (the most recently added component)
    const lastComponent = richContainer.lastElementChild as HTMLElement;
    if (!lastComponent) return;

    // A restored response may be much taller than the available viewport. Use
    // the actual scroll container instead of scrollIntoView(), which can align
    // the last bubble above the viewport and race with a host resize.
    messagesContainer.scrollTop = messagesContainer.scrollHeight;
    
    // Update scroll indicator after scrolling
    setTimeout(() => this.updateScrollIndicator(), 100);
  }

  /** Scroll a restored conversation to its latest rendered component. */
  scrollToLatestMessage(): void {
    const messagesContainer = this.shadowRoot?.querySelector('.chat-messages') as HTMLElement | null;
    if (!messagesContainer) return;
    const apply = () => {
      const current = this.shadowRoot?.querySelector('.chat-messages') as HTMLElement | null;
      if (!current) return;
      current.scrollTop = current.scrollHeight;
      this.updateScrollIndicator();
    };
    apply();
    // A host resize can settle after the Lit update and after a rich message's
    // custom element finishes layout. Re-apply twice so a restored response is
    // never left a few pixels above its latest line.
    requestAnimationFrame(() => requestAnimationFrame(apply));
  }

  isAtLatestMessage(): boolean {
    const messagesContainer = this.shadowRoot?.querySelector('.chat-messages') as HTMLElement | null;
    if (!messagesContainer) return false;
    return messagesContainer.scrollTop + messagesContainer.clientHeight >= messagesContainer.scrollHeight - 4;
  }

  /**
   * ComponentManager renders custom elements asynchronously. A few animation
   * frames make the restored message height and the embedded host height settle
   * before choosing the final scroll position.
   */
  private scheduleChartIntoView(): void {
    let attempts = 0;
    const settle = () => {
      attempts += 1;
      const messages = this.shadowRoot?.querySelector('.chat-messages') as HTMLElement | null;
      const charts = this.shadowRoot?.querySelectorAll('.rich-chart');
      const chart = charts && charts.length ? charts[charts.length - 1] as HTMLElement : null;
      if (messages && chart) {
        const delta = chart.getBoundingClientRect().top - messages.getBoundingClientRect().top;
        messages.scrollTop += delta - 8;
        return;
      }
      if (attempts < 8) requestAnimationFrame(settle);
    };
    requestAnimationFrame(settle);
  }

  private scheduleLatestMessageScroll(): void {
    let attempts = 0;
    const settle = () => {
      attempts += 1;
      const messagesContainer = this.shadowRoot?.querySelector('.chat-messages');
      const richContainer = this.shadowRoot?.querySelector('.rich-components-container');
      if (messagesContainer && richContainer?.children.length) {
        this.scrollToLatestMessage();
      }
      if (attempts < 6) {
        requestAnimationFrame(settle);
      }
    };
    requestAnimationFrame(settle);
  }

  /**
   * Clear all messages (useful for testing)
   */
  clearMessages() {
    if (this.componentManager) {
      this.componentManager.clear();
    }
    this.updateEmptyState();
    this.requestUpdate();
  }

  /**
   * Add multiple messages at once (useful for testing scrolling)
   */
  addTestMessages(count: number = 10) {
    for (let i = 1; i <= count; i++) {
      setTimeout(() => {
        const type = i % 2 === 0 ? 'assistant' : 'user';
        const content = `This is test message number ${i}. Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.`;
        this.addMessage(content, type);
      }, i * 100); // Stagger the messages to simulate real timing
    }
  }

  render() {
    return html`
      <!-- Minimized icon - shown only when minimized via CSS and allowMinimize is true -->
      ${this.allowMinimize ? html`
        <div class="minimized-icon" @click=${this.restoreWindow}>
          <svg viewBox="0 0 24 24" fill="currentColor" width="32" height="32">
            <path d="M20 2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h14l4 4V4c0-1.1-.9-2-2-2zm-2 12H6v-2h12v2zm0-3H6V9h12v2zm0-3H6V6h12v2z"/>
          </svg>
        </div>
      ` : ''}

      <!-- Main chat interface -->
      <div class="chat-layout ${this.showProgress ? '' : 'compact'}">
        <div class="chat-main">
          <div class="chat-header">
            <div class="header-top">
              <div class="header-left">
                <div class="chat-avatar" aria-hidden="true">${this.getTitleInitials()}</div>
                <div class="header-text">
                  <h2 class="chat-title">${this.title}</h2>
                </div>
              </div>
              <div class="header-top-actions">
                <div class="window-controls">
                  ${this.allowMinimize ? html`
                    <button
                      class="window-control-btn minimize"
                      @click=${this.minimizeWindow}
                      title=${this.minimizeTitle}>
                      <svg viewBox="0 0 24 24" fill="currentColor">
                        <path d="M5 12h14v2H5z"/>
                      </svg>
                    </button>
                  ` : ''}
                  ${this.windowState === 'maximized' ? html`
                    <button
                      class="window-control-btn restore"
                      @click=${this.restoreWindow}
                      title=${this.restoreTitle}>
                      <svg viewBox="0 0 24 24" fill="currentColor">
                        <path d="M8 8v2h2V8h6v6h-2v2h4V6H8zm-2 4v8h8v-2H8v-6H6z"/>
                      </svg>
                    </button>
                  ` : html`
                    <button
                      class="window-control-btn maximize"
                      @click=${this.maximizeWindow}
                      title=${this.maximizeTitle}>
                      <svg viewBox="0 0 24 24" fill="currentColor">
                        <path d="M5 5v14h14V5H5zm2 2h10v10H7V7z"/>
                      </svg>
                    </button>
                  `}
                </div>
              </div>
            </div>
          </div>

          <div class="chat-messages">
            <!-- Empty state - shown when no components exist -->
            <div class="empty-state" id="empty-state">
              <div class="empty-state-icon">
                <svg viewBox="0 0 24 24" fill="currentColor">
                  <path d="M20 2H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h14l4 4V4c0-1.1-.9-2-2-2zm-2 12H6v-2h12v2zm0-3H6V9h12v2zm0-3H6V6h12v2z"/>
                </svg>
              </div>
              <div class="empty-state-text">${this.emptyStateTitle}</div>
              <div class="empty-state-subtitle">${this.emptyStateSubtitle}</div>
            </div>

            <!-- Rich Components Container - all content renders here via ComponentManager -->
            <div class="rich-components-container"></div>
          </div>

          <div class="chat-input-area">
            <vanna-status-bar
              .status=${this.status}
              .message=${this.statusMessage}
              .detail=${this.statusDetail}
              theme=${this.theme}>
            </vanna-status-bar>

            <div class="chat-input-container">
              <textarea
                class="message-input"
                .placeholder=${this.placeholder}
                .disabled=${this.disabled}
                @input=${this.handleInput}
                @keydown=${this.handleKeyPress}
                rows="1"
              ></textarea>
              ${this.sendInFlight ? html`<button class="send-button" type="button"
                aria-label="停止生成" .disabled=${this.stopping} @click=${() => this.stopResponse()}>
                ${this.stopping ? '…' : '■'}
              </button>` : html`<button
                class="send-button"
                type="button"
                aria-label=${this.sendButtonLabel}
                .disabled=${this.disabled || !this.currentMessage.trim()}
                @click=${this.sendMessage}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
                </svg>
              </button>`}
            </div>
          </div>
        </div>

        ${this.showProgress ? html`
          <div class="sidebar">
            <vanna-progress-tracker theme=${this.theme}></vanna-progress-tracker>
          </div>
        ` : ''}
      </div>
    `;
  }
}
