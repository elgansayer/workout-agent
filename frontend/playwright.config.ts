import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  workers: 2,
  use: { baseURL: 'http://127.0.0.1:4200', trace: 'retain-on-failure', reducedMotion: 'reduce' },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1440, height: 1000 } } },
    { name: 'mobile', use: { viewport: { width: 390, height: 844 } } },
    { name: 'narrow', use: { viewport: { width: 320, height: 740 } } },
  ],
  webServer: {
    command: 'npm start -- --host 127.0.0.1 --port 4200 --proxy-config e2e/proxy.conf.json',
    url: 'http://127.0.0.1:4200',
    reuseExistingServer: !process.env['CI'],
    timeout: 120000,
  },
});
