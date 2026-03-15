declare global {
  interface Window {
    Telegram?: {
      WebApp: {
        initData: string;
        close: () => void;
        sendData: (data: string) => void;
      };
    };
  }
}

export function getInitData(): string {
  const initData = window.Telegram?.WebApp?.initData;
  if (!initData) {
    throw new Error("Telegram WebApp initData is not available");
  }
  return initData;
}

export function getAuthHeader(): Record<string, string> {
  const initData = getInitData();
  return {
    Authorization: `tg-init-data ${initData}`,
  };
}
