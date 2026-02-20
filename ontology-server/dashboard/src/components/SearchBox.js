import React, { useState, useRef, useEffect } from 'react';
import styled from 'styled-components';
import { useNavigate } from 'react-router-dom';
import { useSearch } from '../hooks/useEntities';
import { entityTypeColor } from '../styles/theme';
import { entityLabel } from '../utils/labels';

const Wrap = styled.div`position: relative; flex: 1; max-width: 480px;`;

const Input = styled.input`
  width: 100%;
  padding: 10px 16px 10px 40px;
  font-size: 0.9rem;
  border-radius: ${({ theme }) => theme.radii.md};
  background: ${({ theme }) => theme.colors.surface};
  border: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  &:focus {
    border-color: ${({ theme }) => theme.colors.accent};
    box-shadow: ${({ theme }) => theme.shadows.glow(theme.colors.accent)};
  }
`;

const Icon = styled.span`
  position: absolute;
  left: 14px;
  top: 50%;
  transform: translateY(-50%);
  color: ${({ theme }) => theme.colors.textMuted};
  font-size: 0.9rem;
  pointer-events: none;
`;

const Dropdown = styled.div`
  position: absolute;
  top: calc(100% + 4px);
  left: 0; right: 0;
  background: ${({ theme }) => theme.colors.surface};
  border: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  border-radius: ${({ theme }) => theme.radii.md};
  box-shadow: ${({ theme }) => theme.shadows.lg};
  max-height: 360px;
  overflow-y: auto;
  z-index: 200;
  animation: fadeIn 150ms ease;
`;

const Item = styled.div`
  padding: 10px 16px;
  display: flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  transition: background ${({ theme }) => theme.transitions.fast};
  &:hover { background: ${({ theme }) => theme.colors.surfaceHover}; }
`;

const TypeDot = styled.span`
  width: 8px; height: 8px;
  border-radius: 50%;
  background: ${({ $color }) => $color};
  flex-shrink: 0;
`;

const Name = styled.span`font-weight: 500; font-size: 0.875rem;`;
const TypeLabel = styled.span`font-size: 0.75rem; color: ${({ theme }) => theme.colors.textSecondary};`;

export default function SearchBox() {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const { results, search } = useSearch();
  const navigate = useNavigate();
  const ref = useRef();

  useEffect(() => {
    const t = setTimeout(() => search(query), 250);
    return () => clearTimeout(t);
  }, [query, search]);

  useEffect(() => {
    const handler = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && query.trim()) {
      setOpen(false);
      navigate(`/search?q=${encodeURIComponent(query.trim())}`);
    }
  };

  return (
    <Wrap ref={ref}>
      <Icon>🔍</Icon>
      <Input
        placeholder="Search entities..."
        value={query}
        onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
        onFocus={() => query && setOpen(true)}
        onKeyDown={handleKeyDown}
        aria-label="Search entities"
      />
      {open && results.length > 0 && (
        <Dropdown>
          {results.map((r) => (
            <Item key={r.id} onClick={() => { setOpen(false); setQuery(''); navigate(`/entity/${r.id}`); }}>
              <TypeDot $color={entityTypeColor(r.type)} />
              <Name>{entityLabel(r)}</Name>
              <TypeLabel>{r.type}</TypeLabel>
            </Item>
          ))}
        </Dropdown>
      )}
    </Wrap>
  );
}
