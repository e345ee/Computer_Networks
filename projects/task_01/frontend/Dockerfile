FROM node:20-alpine AS build
WORKDIR /app

COPY package.json ./
RUN npm config set registry https://registry.npmjs.org/ \
    && npm install --include=dev --no-audit --no-fund

COPY . .
RUN ./node_modules/.bin/tsc && ./node_modules/.bin/vite build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
