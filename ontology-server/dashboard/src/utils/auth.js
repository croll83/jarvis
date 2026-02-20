const TOKEN_KEY = 'ontology_token';
const SPEAKER_KEY = 'ontology_speaker';

export const getToken = () => localStorage.getItem(TOKEN_KEY) || '';
export const setToken = (token) => localStorage.setItem(TOKEN_KEY, token);
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);
export const hasToken = () => !!localStorage.getItem(TOKEN_KEY);

export const getSpeaker = () => localStorage.getItem(SPEAKER_KEY) || '';
export const setSpeaker = (id) => localStorage.setItem(SPEAKER_KEY, id);
export const clearSpeaker = () => localStorage.removeItem(SPEAKER_KEY);
