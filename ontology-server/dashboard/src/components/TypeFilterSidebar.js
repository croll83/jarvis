import React from 'react';
import styled from 'styled-components';
import { entityTypeColor } from '../styles/theme';

const Sidebar = styled.aside`
  width: 220px;
  flex-shrink: 0;
  background: ${({ theme }) => theme.colors.surface};
  border-right: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  padding: 20px 16px;
  overflow-y: auto;
  @media (max-width: 768px) {
    width: 100%;
    border-right: none;
    border-bottom: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
    padding: 12px 16px;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
`;

const Title = styled.h3`
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 1.5px;
  color: ${({ theme }) => theme.colors.textMuted};
  margin-bottom: 16px;
  @media (max-width: 768px) { display: none; }
`;

const FilterItem = styled.label`
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border-radius: ${({ theme }) => theme.radii.sm};
  cursor: pointer;
  font-size: 0.85rem;
  transition: background ${({ theme }) => theme.transitions.fast};
  &:hover { background: ${({ theme }) => theme.colors.surfaceHover}; }
  @media (max-width: 768px) { padding: 6px 10px; }
`;

const Dot = styled.span`
  width: 10px; height: 10px;
  border-radius: 50%;
  background: ${({ $color }) => $color};
`;

const Check = styled.input.attrs({ type: 'checkbox' })`
  accent-color: ${({ theme }) => theme.colors.accent};
  width: 14px; height: 14px;
`;

const Count = styled.span`
  margin-left: auto;
  font-size: 0.7rem;
  color: ${({ theme }) => theme.colors.textMuted};
  font-family: ${({ theme }) => theme.fonts.mono};
`;

const ENTITY_TYPES = ['Person', 'Account', 'Business', 'Location', 'Task', 'Event', 'Project', 'Interest', 'Tag'];

export default function TypeFilterSidebar({ selected = [], onChange, counts = {} }) {
  const toggle = (type) => {
    onChange(selected.includes(type) ? selected.filter((t) => t !== type) : [...selected, type]);
  };

  return (
    <Sidebar aria-label="Entity type filters">
      <Title>Entity Types</Title>
      {ENTITY_TYPES.map((type) => (
        <FilterItem key={type}>
          <Check checked={selected.includes(type)} onChange={() => toggle(type)} />
          <Dot $color={entityTypeColor(type)} />
          <span>{type}</span>
          {counts[type] != null && <Count>{counts[type]}</Count>}
        </FilterItem>
      ))}
    </Sidebar>
  );
}
