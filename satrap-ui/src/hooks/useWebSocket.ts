import { useEffect, useRef, useState, useCallback } from 'react';
import { SatrapWebSocket, WebSocketEventHandler, WebSocketEndpoint } from '@/api/websocket';

export function useWebSocket(endpoint: WebSocketEndpoint) {
  const wsRef = useRef<SatrapWebSocket | null>(null);
  const [isConnected, setIsConnected] = useState(false);

  const connect = useCallback((handlers: WebSocketEventHandler) => {
    if (wsRef.current) {
      wsRef.current.disconnect();
    }

    wsRef.current = new SatrapWebSocket(endpoint);
    wsRef.current.connect({
      ...handlers,
      onConnect: () => {
        setIsConnected(true);
        handlers.onConnect?.();
      },
      onDisconnect: () => {
        setIsConnected(false);
        handlers.onDisconnect?.();
      },
    });
  }, [endpoint]);

  const disconnect = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.disconnect();
      wsRef.current = null;
      setIsConnected(false);
    }
  }, []);

  useEffect(() => {
    return () => {
      disconnect();
    };
  }, [disconnect]);

  return {
    connect,
    disconnect,
    isConnected,
  };
}
