import { useState, useEffect, useCallback } from 'react';
import { getToken, setToken, clearToken, hasToken, getSpeaker, setSpeaker, clearSpeaker } from '../utils/auth';
import { validateToken, fetchSpeakers } from '../services/api';

export function useAuth() {
  const [token, setTokenState] = useState(getToken());
  const [isValid, setIsValid] = useState(null); // null=unknown, true/false
  const [speaker, setSpeakerState] = useState(getSpeaker());
  const [speakers, setSpeakers] = useState([]);
  const [loading, setLoading] = useState(false);

  const authenticate = useCallback(async (newToken) => {
    setToken(newToken);
    setTokenState(newToken);
    setLoading(true);
    try {
      await validateToken();
      setIsValid(true);
      // Load speakers for impersonation
      try {
        const res = await fetchSpeakers();
        setSpeakers(Array.isArray(res.data) ? res.data : []);
      } catch { setSpeakers([]); }
    } catch {
      setIsValid(false);
    } finally {
      setLoading(false);
    }
  }, []);

  const logout = useCallback(() => {
    clearToken();
    clearSpeaker();
    setTokenState('');
    setSpeakerState('');
    setIsValid(null);
    setSpeakers([]);
  }, []);

  const impersonate = useCallback((id) => {
    setSpeaker(id);
    setSpeakerState(id);
  }, []);

  const resetImpersonation = useCallback(() => {
    clearSpeaker();
    setSpeakerState('');
  }, []);

  // Validate on mount if token exists
  useEffect(() => {
    if (hasToken()) authenticate(getToken());
    const handler = () => setIsValid(false);
    window.addEventListener('auth:invalid', handler);
    return () => window.removeEventListener('auth:invalid', handler);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return { token, isValid, loading, speaker, speakers, authenticate, logout, impersonate, resetImpersonation };
}
