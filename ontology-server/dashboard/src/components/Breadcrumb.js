import React from 'react';
import styled from 'styled-components';
import { Link } from 'react-router-dom';

const Bar = styled.nav`
  background: ${({ theme }) => theme.colors.bg};
  border-bottom: 1px solid ${({ theme }) => theme.colors.surfaceBorder};
  padding: 8px 24px;
  font-size: 0.8rem;
  display: flex;
  align-items: center;
  gap: 6px;
  color: ${({ theme }) => theme.colors.textSecondary};
  overflow-x: auto;
  white-space: nowrap;
`;

const Crumb = styled(Link)`
  color: ${({ theme }) => theme.colors.textSecondary};
  &:hover { color: ${({ theme }) => theme.colors.accent}; }
`;

const Active = styled.span`
  color: ${({ theme }) => theme.colors.text};
  font-weight: 500;
`;

const Sep = () => <span style={{ opacity: 0.4 }}>›</span>;

export default function Breadcrumb({ items = [] }) {
  if (!items.length) return null;
  return (
    <Bar aria-label="Breadcrumb">
      <Crumb to="/">Home</Crumb>
      {items.map((item, i) => (
        <React.Fragment key={i}>
          <Sep />
          {i < items.length - 1 ? (
            <Crumb to={item.path}>{item.label}</Crumb>
          ) : (
            <Active>{item.label}</Active>
          )}
        </React.Fragment>
      ))}
    </Bar>
  );
}
