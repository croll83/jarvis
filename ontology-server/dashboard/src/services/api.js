import axios from 'axios';
import { getToken, getSpeaker } from '../utils/auth';

// In production, API is on same origin (served by same FastAPI server)
const BASE_URL = window.__RUNTIME_CONFIG__?.API_URL || process.env.REACT_APP_API_URL || window.location.origin;

const api = axios.create({ baseURL: BASE_URL, timeout: 15000 });

api.interceptors.request.use((config) => {
  const token = getToken();
  if (token) config.headers['Authorization'] = `Bearer ${token}`;
  const speaker = getSpeaker();
  if (speaker) config.headers['X-Speaker-Id'] = speaker;
  return config;
});

api.interceptors.response.use(
  (r) => r,
  (err) => {
    if (err.response?.status === 401) {
      window.dispatchEvent(new CustomEvent('auth:invalid'));
    }
    return Promise.reject(err);
  }
);

// Entities
export const fetchEntities = (params = {}) => api.get('/entities', { params });
export const fetchEntity = (id) => api.get(`/entities/${id}`);
export const fetchRelated = (id, dir = 'both') => api.get(`/entities/${id}/related`, { params: { dir } });
export const searchEntities = (query) => api.post('/entities/query', query);
export const createEntity = (data) => api.post('/entities', data);
export const updateEntity = (id, data) => api.patch(`/entities/${id}`, data);
export const deleteEntity = (id) => api.delete(`/entities/${id}`);

// Schema & helpers
export const fetchSchema = () => api.get('/schema');
export const fetchShortestPath = (fromId, toId) => api.get('/helpers/path', { params: { from_id: fromId, to_id: toId } });

// Speakers (for impersonation)
export const fetchSpeakers = () => api.get('/speakers');

// Validate token
export const validateToken = () => api.get('/entities', { params: { limit: 1 } });

export default api;
