import { API_BASE_URL } from '@/utils/constants';

export interface LogMessage {
  type: 'log';
  data: {
    content: string;
    level: string;
  };
}

export interface StatusMessage {
  type: 'status';
  data: {
    running: boolean;
    adapters: Record<string, {
      status: string;
      started: boolean;
      config_type?: string;
      type?: string;
      last_error?: string;
    }>;
  };
}

export interface ErrorMessage {
  type: 'error';
  message: string;
}

export type WebSocketMessage = LogMessage | StatusMessage | ErrorMessage;

export type WebSocketEventHandler = {
  onLog?: (data: LogMessage['data']) => void;
  onStatus?: (data: StatusMessage['data']) => void;
  onError?: (message: string) => void;
  onConnect?: () => void;
  onDisconnect?: () => void;
};

export type WebSocketEndpoint = '/ws/status' | '/ws/logs' | `/ws/logs?lines=${number}`;

export class SatrapWebSocket {
  private ws: WebSocket | null = null;
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;
  private reconnectDelay = 1000;
  private handlers: WebSocketEventHandler = {};
  private endpoint: string;
  private isIntentionallyClosed = false;

  constructor(endpoint: WebSocketEndpoint) {
    this.endpoint = endpoint;
  }

  connect(handlers: WebSocketEventHandler) {
    this.handlers = handlers;
    this.isIntentionallyClosed = false;
    this._connect();
  }

  private _connect() {
    try {
      const wsUrl = API_BASE_URL.replace(/^http/, 'ws') + this.endpoint;
      this.ws = new WebSocket(wsUrl);

      this.ws.onopen = () => {
        console.log(`[WebSocket] Connected to ${this.endpoint}`);
        this.reconnectAttempts = 0;
        this.handlers.onConnect?.();
      };

      this.ws.onmessage = (event) => {
        try {
          const message: WebSocketMessage = JSON.parse(event.data);
          this._handleMessage(message);
        } catch (e) {
          console.error('[WebSocket] Failed to parse message:', e);
        }
      };

      this.ws.onclose = (event) => {
        console.log(`[WebSocket] Disconnected from ${this.endpoint}`, event.code, event.reason);
        this.handlers.onDisconnect?.();
        
        if (!this.isIntentionallyClosed && this.reconnectAttempts < this.maxReconnectAttempts) {
          this._scheduleReconnect();
        }
      };

      this.ws.onerror = (error) => {
        console.error('[WebSocket] Error:', error);
        this.handlers.onError?.('WebSocket connection error');
      };
    } catch (e) {
      console.error('[WebSocket] Failed to create connection:', e);
      this._scheduleReconnect();
    }
  }

  private _handleMessage(message: WebSocketMessage) {
    switch (message.type) {
      case 'log':
        this.handlers.onLog?.(message.data);
        break;
      case 'status':
        this.handlers.onStatus?.(message.data);
        break;
      case 'error':
        this.handlers.onError?.(message.message);
        break;
    }
  }

  private _scheduleReconnect() {
    const delay = this.reconnectDelay * Math.pow(2, this.reconnectAttempts);
    console.log(`[WebSocket] Reconnecting in ${delay}ms... (attempt ${this.reconnectAttempts + 1})`);
    
    setTimeout(() => {
      this.reconnectAttempts++;
      this._connect();
    }, delay);
  }

  disconnect() {
    this.isIntentionallyClosed = true;
    if (this.ws) {
      this.ws.close(1000, 'Client disconnected');
      this.ws = null;
    }
  }

  isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }
}
