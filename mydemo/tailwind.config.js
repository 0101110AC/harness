/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./src/**/*.{html,ts}'],
  theme: {
    extend: {
      colors: {
        primary: {
          DEFAULT: '#1890FF',
          hover: '#096DD9',
          active: '#0050B3',
          soft: '#E6F4FF',
          border: '#91CAFF',
        },
        danger: {
          DEFAULT: '#F5222D',
          hover: '#CF1322',
          soft: '#FFF1F0',
          border: '#FFA39E',
        },
        success: {
          DEFAULT: '#52C41A',
          soft: '#F6FFED',
          border: '#B7EB8F',
        },
        warning: {
          DEFAULT: '#FAAD14',
          soft: '#FFFBE6',
          border: '#FFE58F',
        },
        canvas: '#F7F8FA',
        // PRD prd-01 §5：批注状态色（全站统一，禁止自定义）
        annotation: {
          pending: '#FAAD14',
          issued: '#1890FF',
          modified: '#722ED1',
          closed: '#52C41A',
        },
        // PRD prd-01 §5：严重度色
        severity: {
          blocker: '#F5222D',
          major: '#FA8C16',
          minor: '#FAAD14',
          info: '#8C8C8C',
        },
      },
      fontFamily: {
        sans: [
          'Inter',
          'system-ui',
          '-apple-system',
          'Segoe UI',
          'Roboto',
          'PingFang SC',
          'Hiragino Sans GB',
          'Microsoft YaHei',
          'sans-serif',
        ],
        mono: ['JetBrains Mono', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      borderRadius: {
        card: '8px',
      },
      boxShadow: {
        card: '0 1px 2px rgba(15, 23, 42, 0.04), 0 1px 3px rgba(15, 23, 42, 0.04)',
        float: '0 12px 32px -12px rgba(15, 23, 42, 0.18)',
        focus: '0 0 0 4px rgba(24, 144, 255, 0.12)',
      },
      spacing: {
        sidebar: '220px',
        'sidebar-collapsed': '64px',
        topbar: '56px',
      },
      maxWidth: {
        content: '1440px',
      },
      transitionTimingFunction: {
        standard: 'cubic-bezier(0.4, 0, 0.2, 1)',
      },
      keyframes: {
        'toast-in': {
          from: { opacity: '0', transform: 'translateY(-8px) scale(0.98)' },
          to: { opacity: '1', transform: 'translateY(0) scale(1)' },
        },
        'fade-in': {
          from: { opacity: '0', transform: 'translateY(-4px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: {
        'toast-in': 'toast-in 180ms cubic-bezier(0.4, 0, 0.2, 1)',
        'fade-in': 'fade-in 140ms cubic-bezier(0.4, 0, 0.2, 1)',
      },
    },
  },
  plugins: [],
};
