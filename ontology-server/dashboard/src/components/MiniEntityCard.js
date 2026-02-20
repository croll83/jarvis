import React from 'react';
import styled from 'styled-components';
import { Link } from 'react-router-dom';
import { entityTypeColor } from '../styles/theme';
import { entityLabel } from '../utils/labels';

const Card = styled(Link)`
  display: block;
  background: ${({ theme }) => theme.colors.surface};
  border: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  border-radius: ${({ theme }) => theme.radii.md};
  padding: 16px;
  transition: all ${({ theme }) => theme.transitions.normal};
  animation: fadeIn 300ms ease both;
  animation-delay: ${({ $i }) => ($i || 0) * 40}ms;
  &:hover {
    border-color: ${({ $color }) => $color};
    box-shadow: ${({ $color }) => `0 0 20px ${$color}25`};
    transform: translateY(-2px);
  }
`;

const TopRow = styled.div`display: flex; align-items: center; gap: 10px; margin-bottom: 8px;`;

const TypeBadge = styled.span`
  font-size: 0.65rem;
  padding: 2px 8px;
  border-radius: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  background: ${({ $color }) => $color + '20'};
  color: ${({ $color }) => $color};
`;

const Name = styled.h3`
  font-size: 0.95rem;
  font-weight: 600;
  color: ${({ theme }) => theme.colors.text};
  margin-bottom: 4px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
`;

const Id = styled.span`
  font-family: ${({ theme }) => theme.fonts.mono};
  font-size: 0.7rem;
  color: ${({ theme }) => theme.colors.textMuted};
`;

const Props = styled.div`
  margin-top: 8px;
  font-size: 0.8rem;
  color: ${({ theme }) => theme.colors.textSecondary};
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
`;

export default function MiniEntityCard({ entity, index }) {
  const color = entityTypeColor(entity.type);
  const name = entityLabel(entity);
  const propEntries = Object.entries(entity.properties || {}).filter(([k]) => k !== 'name' && k !== 'title');

  return (
    <Card to={`/entity/${entity.id}`} $color={color} $i={index}>
      <TopRow>
        <TypeBadge $color={color}>{entity.type}</TypeBadge>
        <Id>{entity.id}</Id>
      </TopRow>
      <Name>{name}</Name>
      {propEntries.length > 0 && (
        <Props>
          {propEntries.slice(0, 3).map(([k, v]) => (
            <span key={k}>{k}: {String(v)} · </span>
          ))}
        </Props>
      )}
    </Card>
  );
}
