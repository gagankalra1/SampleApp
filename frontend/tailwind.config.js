/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Deep slate base with an electric indigo/violet accent. The palette is
        // intentionally narrow: two accents and a neutral ramp.
        ink: {
          950: '#07080f',
          900: '#0b0d17',
          850: '#111320',
          800: '#171a2b',
          700: '#232741',
          600: '#333a5c',
          500: '#4a5280',
        },
        volt: {
          300: '#a5b4fc',
          400: '#818cf8',
          500: '#6366f1',
          600: '#4f46e5',
        },
        spark: {
          400: '#22d3ee',
          500: '#06b6d4',
        },
      },
      fontFamily: {
        sans: ['Inter var', 'Inter', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
      },
      boxShadow: {
        glow: '0 0 0 1px rgb(99 102 241 / 0.25), 0 10px 40px -12px rgb(99 102 241 / 0.45)',
        card: '0 1px 2px rgb(0 0 0 / 0.4), 0 8px 24px -16px rgb(0 0 0 / 0.6)',
      },
      animation: {
        'fade-up': 'fadeUp 280ms cubic-bezier(0.16, 1, 0.3, 1)',
        'slide-up': 'slideUp 240ms cubic-bezier(0.16, 1, 0.3, 1)',
        shimmer: 'shimmer 1.6s linear infinite',
      },
      keyframes: {
        fadeUp: {
          '0%': { opacity: '0', transform: 'translateY(8px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        slideUp: {
          '0%': { opacity: '0', transform: 'translateY(16px) scale(0.98)' },
          '100%': { opacity: '1', transform: 'translateY(0) scale(1)' },
        },
        shimmer: {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
      },
    },
  },
  plugins: [],
}
