export const environment = {
  production: true,
  // Leave empty for same-origin / reverse-proxy setups, or set to e.g. "https://api.workout.domain.com"
  apiUrl: (typeof window !== 'undefined' && (window as any).__WORKOUT_AGENT_API_URL) || '',
};
