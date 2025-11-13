# Sagasu 4 - React Frontend

Modern, responsive React frontend for Sagasu 4 room availability finder.

## Features

- 🎨 **Spacemacs Color Schemes** - Light and dark mode with authentic Spacemacs colors
- ⚡ **Real-time Data** - Fetches latest room availability from GitHub
- 🔍 **Advanced Filtering** - Search, filter by building/floor/facility type
- ⭐ **Favorites** - Save favorite rooms to localStorage
- 📱 **Fully Responsive** - Optimized for mobile and desktop
- 🚀 **Fast & Lightweight** - Built with Vite and TailwindCSS

## Tech Stack

- **React 19** - UI library
- **Vite** - Build tool
- **TailwindCSS** - Styling
- **Custom Hooks** - State management
- **GitHub Raw** - Data source (no backend needed)

## Development

### Prerequisites

- Node.js 20+
- npm or yarn

### Setup

```bash
# Install dependencies
npm install

# Start dev server
npm run dev

# Build for production
npm run build

# Preview production build
npm run preview
```

### Environment

The app fetches data from:
- Room data: `https://raw.githubusercontent.com/gongahkia/sagasu-4/main/backend/log/scraped_log.json`
- Booking data: `https://raw.githubusercontent.com/gongahkia/sagasu-4/main/backend/log/bookings_log.json`

## Deployment

### Vercel (Recommended)

1. Push to GitHub
2. Import project on [Vercel](https://vercel.com)
3. Set build settings:
   - Framework Preset: Vite
   - Build Command: `npm run build`
   - Output Directory: `dist`
   - Root Directory: `frontend-react`
4. Deploy

### Netlify

1. Push to GitHub
2. Create new site on [Netlify](https://netlify.com)
3. Build settings:
   - Base Directory: `frontend-react`
   - Build Command: `npm run build`
   - Publish Directory: `frontend-react/dist`
4. Deploy

## Project Structure

```
frontend-react/
├── src/
│   ├── components/      # React components
│   │   ├── Header.jsx
│   │   ├── Statistics.jsx
│   │   ├── Filters.jsx
│   │   ├── RoomCard.jsx
│   │   └── BookingCard.jsx
│   ├── hooks/          # Custom hooks
│   │   ├── useTheme.js
│   │   ├── useRoomData.js
│   │   └── useFavorites.js
│   ├── utils/          # Utility functions
│   │   ├── filters.js
│   │   └── time.js
│   ├── App.jsx         # Main app component
│   ├── main.jsx        # Entry point
│   └── index.css       # Global styles
├── public/             # Static assets
├── index.html          # HTML template
├── vite.config.js      # Vite configuration
├── tailwind.config.js  # Tailwind configuration
├── postcss.config.js   # PostCSS configuration
└── vercel.json         # Vercel configuration
```

## Features in Detail

### Theme Toggle

Spacemacs light and dark modes with smooth transitions. Theme preference saved to localStorage.

### Room Filtering

- **Search** - Filter by room name or building
- **Availability** - Show all, available only, or booked only
- **Buildings** - Multi-select building filter
- **Floors** - Multi-select floor filter
- **Facility Types** - Multi-select facility type filter
- **Favorites** - Show only favorited rooms

### Room Cards

- Current availability status
- Free duration and slot count
- Equipment list
- Expandable timeslot details
- Click booked slots to see booking details
- Favorite star button

### Booking Dashboard

- Shows auto-booking status (future feature)
- Confirmed, pending, and failed booking counts
- Recent booking list with details

## Customization

### Colors

Edit `tailwind.config.js` to customize Spacemacs colors.

### Data Source

Edit `src/hooks/useRoomData.js` to change data source URLs.

## License

MIT
