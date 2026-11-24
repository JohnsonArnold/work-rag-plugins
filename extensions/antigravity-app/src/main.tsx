import React from 'react';
import { createRoot } from 'react-dom/client';
import { WorkRagApp } from './App';

const container = document.getElementById('root');
if (container) {
  createRoot(container).render(<WorkRagApp />);
}
