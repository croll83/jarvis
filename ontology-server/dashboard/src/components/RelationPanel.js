import React, { useState, useMemo } from 'react';
import styled from 'styled-components';
import { Link } from 'react-router-dom';
import { entityTypeColor } from '../styles/theme';

const Panel = styled.div`
  background: ${({ theme }) => theme.colors.surface};
  border: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  border-radius: ${({ theme }) => theme.radii.lg};
  overflow: hidden;
  margin-top: 20px;
  animation: fadeIn 300ms ease 100ms both;
`;

const Header = styled.div`
  padding: 16px 24px;
  border-bottom: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  font-weight: 600;
  font-size: 0.9rem;
`;

const Tabs = styled.div`
  display: flex;
  overflow-x: auto;
  border-bottom: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
`;

const Tab = styled.button`
  padding: 10px 18px;
  font-size: 0.8rem;
  font-weight: 500;
  white-space: nowrap;
  border-bottom: 2px solid ${({ $active, theme }) => $active ? theme.colors.accent : 'transparent'};
  color: ${({ $active, theme }) => $active ? theme.colors.text : theme.colors.textSecondary};
  transition: all ${({ theme }) => theme.transitions.fast};
  &:hover { color: ${({ theme }) => theme.colors.text}; background: ${({ theme }) => theme.colors.surfaceHover}; }
`;

const List = styled.div`max-height: 400px; overflow-y: auto;`;

const RelItem = styled(Link)`
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 24px;
  border-bottom: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  transition: background ${({ theme }) => theme.transitions.fast};
  &:hover { background: ${({ theme }) => theme.colors.surfaceHover}; }
  &:last-child { border-bottom: none; }
`;

const Dot = styled.span`
  width: 8px; height: 8px;
  border-radius: 50%;
  background: ${({ $color }) => $color};
`;

const RelName = styled.span`font-size: 0.875rem; font-weight: 500; color: ${({ theme }) => theme.colors.text};`;
const RelType = styled.span`font-size: 0.7rem; color: ${({ theme }) => theme.colors.textMuted}; margin-left: auto; font-family: ${({ theme }) => theme.fonts.mono};`;
const Arrow = styled.span`font-size: 0.7rem; color: ${({ theme }) => theme.colors.textMuted};`;
const Empty = styled.div`padding: 24px; text-align: center; color: ${({ theme }) => theme.colors.textMuted}; font-size: 0.85rem;`;

export default function RelationPanel({ relations = [], currentId }) {
  const grouped = useMemo(() => {
    const g = {};
    relations.forEach((r) => {
      const key = r.relation || r.type || 'related';
      if (!g[key]) g[key] = [];
      g[key].push(r);
    });
    return g;
  }, [relations]);

  const tabs = Object.keys(grouped);
  const [active, setActive] = useState(tabs[0] || '');

  if (!relations.length) return <Panel><Empty>No relations found</Empty></Panel>;

  const current = grouped[active] || [];

  return (
    <Panel>
      <Header>Relations ({relations.length})</Header>
      <Tabs>
        {tabs.map((t) => (
          <Tab key={t} $active={t === active} onClick={() => setActive(t)}>
            {t} ({grouped[t].length})
          </Tab>
        ))}
      </Tabs>
      <List>
        {current.map((r, i) => {
          const target = r.source_id === currentId
            ? { id: r.target_id, name: r.target_name, type: r.target_type }
            : { id: r.source_id, name: r.source_name, type: r.source_type };
          return (
            <RelItem key={i} to={`/entity/${target.id}`}>
              <Dot $color={entityTypeColor(target.type)} />
              <RelName>{target.name || target.id}</RelName>
              <Arrow>{r.source_id === currentId ? '→' : '←'}</Arrow>
              <RelType>{target.type}</RelType>
            </RelItem>
          );
        })}
      </List>
    </Panel>
  );
}
