export const environment = {
  production: false,
  // Leave empty for same-origin / reverse-proxy setups, or set to e.g. "http://localhost:8000" or "https://api.workout.domain.com"
  apiUrl: (typeof window !== 'undefined' && (window as any).__WORKOUT_AGENT_API_URL) || '',
};
