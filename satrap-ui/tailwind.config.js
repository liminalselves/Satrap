/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  darkMode: ['class', '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        // 背景
        'bg-base': 'var(--color-bg-base)',
        'bg-layer1': 'var(--color-bg-layer1)',
        'bg-layer2': 'var(--color-bg-layer2)',
        'bg-layer3': 'var(--color-bg-layer3)',
        
        // 玻璃材质
        'glass': 'var(--glass-bg)',
        'glass-hover': 'var(--glass-bg-hover)',
        'glass-active': 'var(--glass-bg-active)',
        'glass-border': 'var(--glass-border)',
        'glass-border-hover': 'var(--glass-border-hover)',
        
        // 主色调
        'accent': 'var(--color-accent)',
        'accent-light': 'var(--color-accent-light)',
        'accent-dark': 'var(--color-accent-dark)',
        'accent-subtle': 'var(--color-accent-subtle)',
        
        // 辅助色
        'purple': 'var(--color-purple)',
        'purple-light': 'var(--color-purple-light)',
        'teal': 'var(--color-teal)',
        'teal-light': 'var(--color-teal-light)',
        'pink': 'var(--color-pink)',
        'pink-light': 'var(--color-pink-light)',
        
        // 状态色
        'success': 'var(--color-success)',
        'success-light': 'var(--color-success-light)',
        'warning': 'var(--color-warning)',
        'warning-light': 'var(--color-warning-light)',
        'error': 'var(--color-error)',
        'error-light': 'var(--color-error-light)',
        'info': 'var(--color-info)',
        
        // 文字
        'text-primary': 'var(--color-text-primary)',
        'text-secondary': 'var(--color-text-secondary)',
        'text-tertiary': 'var(--color-text-tertiary)',
        'text-disabled': 'var(--color-text-disabled)',
      },
      borderRadius: {
        'sm': 'var(--radius-sm)',
        'md': 'var(--radius-md)',
        'lg': 'var(--radius-lg)',
        'xl': 'var(--radius-xl)',
      },
      boxShadow: {
        'glass': 'var(--shadow-glass)',
        'glass-hover': 'var(--shadow-glass-hover)',
        'glow-accent': 'var(--shadow-glow-accent)',
        'glow-purple': 'var(--shadow-glow-purple)',
        'glow-teal': 'var(--shadow-glow-teal)',
        'glow-pink': 'var(--shadow-glow-pink)',
      },
      animation: {
        'fade-in': 'fadeIn 200ms ease',
        'slide-in': 'slideIn 250ms cubic-bezier(0.1, 0.9, 0.2, 1)',
        'pulse': 'pulse 2s ease-in-out infinite',
        'shimmer': 'shimmer 2s linear infinite',
        'bg-float': 'bgFloat 30s ease-in-out infinite',
      },
      keyframes: {
        fadeIn: {
          '0%': { opacity: '0' },
          '100%': { opacity: '1' },
        },
        slideIn: {
          '0%': { opacity: '0', transform: 'translateY(-12px) scale(0.96)' },
          '100%': { opacity: '1', transform: 'translateY(0) scale(1)' },
        },
        pulse: {
          '0%, 100%': { transform: 'scale(1)', opacity: '0.3' },
          '50%': { transform: 'scale(1.5)', opacity: '0' },
        },
        shimmer: {
          '0%': { backgroundPosition: '-200% 0' },
          '100%': { backgroundPosition: '200% 0' },
        },
        bgFloat: {
          '0%, 100%': { transform: 'translate(0, 0) rotate(0deg) scale(1)' },
          '25%': { transform: 'translate(2%, 3%) rotate(1deg) scale(1.02)' },
          '50%': { transform: 'translate(-2%, 2%) rotate(-1deg) scale(0.98)' },
          '75%': { transform: 'translate(3%, -2%) rotate(0.5deg) scale(1.01)' },
        },
      },
    },
  },
  plugins: [],
};
