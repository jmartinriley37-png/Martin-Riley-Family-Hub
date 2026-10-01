# Martin-Riley Family Hub — V1 PWA

A mobile-first family organizer prototype/working PWA with parent and daughter experiences.

## Included
- Parent Command Center and simplified Daughter home
- Tasks/chores, Normal/Important/High/Urgent priorities
- Mandatory urgent acknowledgement; optional acknowledgement for High priority
- Complete / not-complete with reason and activity history
- Daughter streaks, points and celebrations
- Family Chore Board
- Ask Parents approval/deny workflow
- Interactive Family Bulletin Board with reactions
- Dance Hub with dedicated Competitions tab
- Family Calendar, Tomorrow Prep, Weekly Recap
- Privacy display and visibility classes
- Offline service worker and installable manifest

## Run
Any static HTTP server works. Example:

    python3 -m http.server 8080

Then open http://localhost:8080

## Important production boundary
This build persists data in browser localStorage for hands-on V1 UX testing. It does NOT yet provide multi-device synchronization, production authentication, server-side authorization, push notifications, or background location. Those require hosted auth/database infrastructure. The UI/data model intentionally separates visibility and roles so those services can replace local persistence without redesigning the product.
