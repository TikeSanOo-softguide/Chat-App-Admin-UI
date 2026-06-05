FROM node:lts AS builder

WORKDIR /app

# Copy package files first
COPY package.json yarn.lock ./

# Enable corepack and install
RUN corepack enable
RUN yarn install

# Copy the rest of the application
COPY . .

EXPOSE 5173

CMD ["yarn", "dev"]