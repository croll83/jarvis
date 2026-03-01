import React from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import { ThemeProvider } from 'styled-components';
import { theme } from './styles/theme';
import GlobalStyles from './styles/GlobalStyles';
import Header from './components/Header';
import { useAuth } from './hooks/useAuth';
import EntityListPage from './pages/EntityListPage';
import EntityDetailPage from './pages/EntityDetailPage';
import GraphPage from './pages/GraphPage';
import SearchPage from './pages/SearchPage';

export default function App() {
  const auth = useAuth();

  return (
    <ThemeProvider theme={theme}>
      <GlobalStyles />
      <BrowserRouter basename="/app">
        <Header auth={auth} />
        <Routes>
          <Route path="/" element={<EntityListPage speaker={auth.speaker} />} />
          <Route path="/entity/:id" element={<EntityDetailPage />} />
          <Route path="/graph" element={<GraphPage speaker={auth.speaker} />} />
          <Route path="/search" element={<SearchPage />} />
        </Routes>
      </BrowserRouter>
    </ThemeProvider>
  );
}
