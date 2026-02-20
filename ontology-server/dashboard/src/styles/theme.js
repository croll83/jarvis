export const theme = {
  colors: {
    bg: '#0D1117',
    surface: '#161B22',
    surfaceHover: '#1C2128',
    surfaceBorder: '#21262D',
    accent: '#6366F1',
    accentHover: '#818CF8',
    accentMuted: 'rgba(99, 102, 241, 0.15)',
    text: '#E6EDF3',
    textSecondary: '#8B949E',
    textMuted: '#484F58',
    success: '#3FB950',
    warning: '#D29922',
    danger: '#F85149',
    info: '#58A6FF',
    // Entity type colors
    entityPerson: '#F97316',
    entityAccount: '#06B6D4',
    entityBusiness: '#8B5CF6',
    entityLocation: '#10B981',
    entityTask: '#EAB308',
    entityEvent: '#EC4899',
    entityDefault: '#6366F1',
  },
  fonts: {
    sans: "'Inter', -apple-system, sans-serif",
    mono: "'JetBrains Mono', monospace",
    display: "'Cal Sans', 'Inter', sans-serif",
  },
  radii: {
    sm: '6px',
    md: '10px',
    lg: '16px',
    xl: '24px',
  },
  shadows: {
    sm: '0 1px 2px rgba(0,0,0,0.3)',
    md: '0 4px 12px rgba(0,0,0,0.4)',
    lg: '0 8px 24px rgba(0,0,0,0.5)',
    glow: (color) => `0 0 20px ${color}40, 0 0 40px ${color}20`,
  },
  transitions: {
    fast: '150ms ease',
    normal: '250ms ease',
    slow: '400ms cubic-bezier(0.4, 0, 0.2, 1)',
  },
};

export const entityTypeColor = (type) => {
  const map = {
    Person: theme.colors.entityPerson,
    Account: theme.colors.entityAccount,
    Business: theme.colors.entityBusiness,
    Location: theme.colors.entityLocation,
    Task: theme.colors.entityTask,
    Event: theme.colors.entityEvent,
  };
  return map[type] || theme.colors.entityDefault;
};
