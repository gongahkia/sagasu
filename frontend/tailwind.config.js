/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Spacemacs Light Mode Colors
        'spacemacs-light': {
          bg: '#fbf8ef',
          'bg-alt': '#efeae9',
          fg: '#655370',
          'fg-alt': '#7c6f64',
          accent: '#6c3163',
          blue: '#4f97d7',
          cyan: '#2d9574',
          green: '#67b11d',
          magenta: '#a31db1',
          orange: '#d26937',
          red: '#f2241f',
          violet: '#a45bad',
          yellow: '#b1951d',
        },
        // Spacemacs Dark Mode Colors
        'spacemacs-dark': {
          bg: '#292b2e',
          'bg-alt': '#212026',
          fg: '#b2b2b2',
          'fg-alt': '#5d4d7a',
          accent: '#4f97d7',
          blue: '#4f97d7',
          cyan: '#2d9574',
          green: '#67b11d',
          magenta: '#a31db1',
          orange: '#d26937',
          red: '#f2241f',
          violet: '#a45bad',
          yellow: '#b1951d',
        },
      },
    },
  },
  plugins: [],
  darkMode: 'class',
}
