import React, { useState } from 'react';
import styled from 'styled-components';
import { Link } from 'react-router-dom';

const HeaderBar = styled.header`
  background: ${({ theme }) => theme.colors.surface};
  border-bottom: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  padding: 0 24px;
  height: 60px;
  display: flex;
  align-items: center;
  gap: 16px;
  position: sticky;
  top: 0;
  z-index: 100;
  backdrop-filter: blur(12px);
`;

const Logo = styled(Link)`
  font-family: ${({ theme }) => theme.fonts.display};
  font-size: 1.25rem;
  font-weight: 700;
  color: ${({ theme }) => theme.colors.text};
  letter-spacing: -0.5px;
  white-space: nowrap;
  &:hover { color: ${({ theme }) => theme.colors.accent}; }
`;

const Spacer = styled.div`flex: 1;`;

const TokenInput = styled.div`
  display: flex;
  align-items: center;
  gap: 8px;
  input {
    width: 200px;
    font-family: ${({ theme }) => theme.fonts.mono};
    font-size: 0.75rem;
    padding: 6px 10px;
    @media (max-width: 768px) { width: 120px; }
  }
`;

const Badge = styled.span`
  font-size: 0.75rem;
  padding: 2px 8px;
  border-radius: 12px;
  font-weight: 600;
  background: ${({ $valid, theme }) => $valid ? theme.colors.success + '20' : theme.colors.danger + '20'};
  color: ${({ $valid, theme }) => $valid ? theme.colors.success : theme.colors.danger};
`;

const Btn = styled.button`
  padding: 6px 14px;
  border-radius: ${({ theme }) => theme.radii.sm};
  font-size: 0.8rem;
  font-weight: 500;
  transition: all ${({ theme }) => theme.transitions.fast};
  background: ${({ $variant, theme }) => $variant === 'accent' ? theme.colors.accent : theme.colors.surfaceBorder};
  color: ${({ theme }) => theme.colors.text};
  &:hover {
    background: ${({ $variant, theme }) => $variant === 'accent' ? theme.colors.accentHover : theme.colors.textMuted};
  }
`;

const SpeakerSelect = styled.select`
  padding: 6px 10px;
  font-size: 0.8rem;
  min-width: 140px;
  background: ${({ theme }) => theme.colors.surface};
  border: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  border-radius: ${({ theme }) => theme.radii.sm};
  color: ${({ theme }) => theme.colors.text};
  cursor: pointer;
  @media (max-width: 768px) { min-width: 100px; }
`;

const SpeakerLabel = styled.span`
  font-size: 0.75rem;
  color: ${({ theme }) => theme.colors.textSecondary};
  white-space: nowrap;
`;

export default function Header({ auth }) {
  const { token, isValid, loading, speaker, speakers, authenticate, logout, impersonate, resetImpersonation } = auth;
  const [input, setInput] = useState('');

  const handleSubmit = (e) => {
    e.preventDefault();
    if (input.trim()) authenticate(input.trim());
  };

  return (
    <HeaderBar>
      <Logo to="/">⬡ Ontology</Logo>
      <Spacer />

      {isValid && speakers.length > 0 && (
        <>
          <SpeakerLabel>Viewing as:</SpeakerLabel>
          <SpeakerSelect
            value={speaker}
            onChange={(e) => e.target.value ? impersonate(e.target.value) : resetImpersonation()}
            aria-label="Impersonate speaker"
          >
            <option value="">— Default —</option>
            {speakers.map((s) => (
              <option key={s.id || s.name} value={s.id || s.name}>
                {s.name || s.id}
              </option>
            ))}
          </SpeakerSelect>
        </>
      )}

      <TokenInput>
        {isValid === true && <Badge $valid>✓ Authenticated</Badge>}
        {isValid === false && <Badge>✗ Invalid</Badge>}
        {!isValid && (
          <form onSubmit={handleSubmit} style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input
              type="password"
              placeholder="Enter API Token"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              aria-label="API Token"
            />
            <Btn type="submit" $variant="accent" disabled={loading}>
              {loading ? '...' : 'Connect'}
            </Btn>
          </form>
        )}
        {isValid && token && (
          <Btn onClick={logout} title="Revoke token">✕</Btn>
        )}
      </TokenInput>
    </HeaderBar>
  );
}
