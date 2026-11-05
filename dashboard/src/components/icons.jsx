/** Small line icons for the overview cards. Stroke follows the text colour. */
const base = {
  width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none',
  stroke: 'currentColor', strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round',
  'aria-hidden': true,
};

export const IconAlert = () => (
  <svg {...base}>
    <path d="M12 3 2.5 20h19L12 3z" />
    <path d="M12 9v5M12 17.2v.3" />
  </svg>
);

export const IconHerd = () => (
  <svg {...base}>
    <path d="M4 8c0-2 1.5-3 3-3h10c1.5 0 3 1 3 3" />
    <path d="M5 8h14l-1 7a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4L5 8z" />
    <path d="M9 13h.01M15 13h.01M10 16h4" />
  </svg>
);

export const IconSignal = () => (
  <svg {...base}>
    <path d="M12 20v-6" />
    <path d="M8.5 10.5a5 5 0 0 1 7 0" />
    <path d="M5.5 7.5a9 9 0 0 1 13 0" />
    <path d="M12 14a1 1 0 1 0 0-2 1 1 0 0 0 0 2z" />
  </svg>
);

export const IconPulse = () => (
  <svg {...base}>
    <path d="M3 12h4l2-5 4 10 2-5h6" />
  </svg>
);

export const IconTag = () => (
  <svg {...base}>
    <path d="M3 12V5a2 2 0 0 1 2-2h7l9 9-9 9-9-9z" />
    <path d="M7.5 7.5h.01" />
  </svg>
);

export const IconClipboard = () => (
  <svg {...base}>
    <path d="M9 4h6v3H9z" />
    <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" />
    <path d="M9 12h6M9 16h4" />
  </svg>
);

export const IconMoon = () => (
  <svg {...base} width={16} height={16}>
    <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" />
  </svg>
);

export const IconSun = () => (
  <svg {...base} width={16} height={16}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </svg>
);

export const IconUsers = () => (
  <svg {...base}>
    <circle cx="9" cy="8" r="3.2" />
    <path d="M3.5 19a5.5 5.5 0 0 1 11 0" />
    <path d="M16 5.5a3 3 0 0 1 0 5.8" />
    <path d="M17.5 13.6a5 5 0 0 1 3 4.6" />
  </svg>
);
